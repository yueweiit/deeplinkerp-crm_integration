"""Manual financial authorization. Deposit agreements stay outside the system."""
import json
import re

import frappe
from frappe import _
from frappe.model import get_permitted_fields
from pypika import Order
from pypika.analytics import RowNumber

from crm_integration.crm_integration.settings import is_crm_integration_enabled

CAPABILITY = "Sales Production Release Permission"
AUDIT_EVENT = "Finance Production Release"
PENDING = "Pending Deposit Confirmation"
PROCESSING = "Deposit Confirmation Processing"
RELEASED_STATES = {"Pending Production", "Pending Final Payment", "Deliverable", "Partially Delivered", "Completed"}


def has_release_permission():
	return bool(frappe.db.exists("DocType", CAPABILITY) and frappe.has_permission(CAPABILITY, "read"))


def readable_fields(doctype, parenttype=None):
	fields = set(get_permitted_fields(doctype, parenttype=parenttype, permission_type="read"))
	meta = frappe.get_meta(doctype)
	levels = meta.get_permlevel_access(permission_type="read", parenttype=parenttype)
	fields.update(df.fieldname for df in meta.fields if df.fieldtype in ("Table", "Table MultiSelect") and df.permlevel in levels)
	return fields


def read_order(name):
	doc = frappe.get_doc("Sales Order", name)
	doc.check_permission("read")
	if not {"company", "customer", "custom_process_status"} <= readable_fields("Sales Order"):
		frappe.throw(_("无权读取放行所需的订单字段。"), frappe.PermissionError)
	frappe.get_doc("Company", doc.company).check_permission("read")
	return doc


def recorded_receipts(doc):
	"""Validate associations internally; expose only readable native PE allocation fields."""
	result = {"receipts": [], "receipts_complete": False, "association_error": False}
	parents = frappe.get_all("Payment Entry Reference", filters={"reference_doctype": "Sales Order", "reference_name": doc.name}, pluck="parent", distinct=True, limit_page_length=0)
	payments = []
	for name in parents:
		try:
			payment = frappe.get_doc("Payment Entry", name)
		except frappe.DoesNotExistError:
			result["association_error"] = True
			continue
		if payment.company != doc.company or payment.party_type != "Customer" or payment.party != doc.customer:
			result["association_error"] = True
			continue
		payments.append(payment)
	pe_fields = {"company", "party_type", "party", "payment_type", "references", "posting_date", "paid_from_account_currency", "paid_to_account_currency"}
	ref_fields = {"reference_doctype", "reference_name", "allocated_amount"}
	if not pe_fields <= readable_fields("Payment Entry") or not ref_fields <= readable_fields("Payment Entry Reference", "Payment Entry"):
		return result
	result["receipts_complete"] = True
	for payment in payments:
		if not frappe.has_permission("Payment Entry", "read", doc=payment):
			result["receipts_complete"] = False
			continue
		allocation = sum(row.allocated_amount for row in payment.references if row.reference_doctype == "Sales Order" and row.reference_name == doc.name)
		result["receipts"].append({"name": payment.name, "posting_date": payment.posting_date, "docstatus": payment.docstatus,
			"payment_type": payment.payment_type, "allocated_amount": allocation,
			"currency": payment.paid_from_account_currency if payment.payment_type == "Receive" else payment.paid_to_account_currency})
	return result


def reject_reason(doc, crm_enabled):
	"""Mirror the rejection endpoint's prerequisites for a read-only review."""
	if not crm_enabled:
		return _("该公司未启用 CRM 集成。")
	if not frappe.has_permission("Sales Order", "cancel", doc=doc):
		return _("没有取消此销售订单的权限，无法驳回。")
	if doc.get("status") == "Closed":
		return _("已关闭的销售订单不能执行生产流程操作，请先重新打开订单。")
	if doc.docstatus != 1:
		return _("只有已提交的销售订单可以驳回并取消。")
	if doc.get("custom_process_status") != PENDING:
		return _("只有待确认定金的销售订单可以驳回。")
	if not doc.get("custom_crm_order_no"):
		return _("缺少 CRM 订单编号，无法推送状态到 CRM。")
	return ""


def mes_push_failure_summary(log):
	"""Return an actionable summary without exposing the MES response or traceback."""
	try:
		response = json.loads(log.get("response_payload") or "{}")
	except (TypeError, ValueError):
		response = {}
	code = response.get("errorCode") if isinstance(response, dict) else None
	if code == "ERP_SALES_ORDER_IDENTITY_CONFLICT":
		return {"code": code, "message": _("MES 订单映射冲突，请核对 ERP/CRM 单号与 MES 既有映射，处理冲突后重试。")}
	if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code):
		return {"code": code, "message": _("MES 拒绝订单下发（错误码：{0}），请联系集成管理员核对日志。").format(code)}
	status = frappe.utils.cint(log.get("http_status_code"))
	if 400 <= status < 600:
		return {"code": f"HTTP {status}", "message": _("MES 订单下发失败（{0}），请联系集成管理员核对日志。").format(f"HTTP {status}")}
	return {"code": "", "message": _("MES 订单下发失败，请联系集成管理员核对日志。")}


def latest_order_logs(doctype, event, names, fields, since=None):
	"""Fetch one latest log per readable order; never load whole histories or old payloads."""
	log = frappe.qb.DocType(doctype)
	rank = RowNumber().over(log.reference_name).orderby(log.creation, order=Order.desc).orderby(log.name, order=Order.desc).as_("row_num")
	query = (frappe.qb.from_(log)
		.select(log.name, rank)
		.where((log.event == event) & (log.reference_doctype == "Sales Order") & log.reference_name.isin(list(names))))
	if since is not None:
		query = query.where(log.creation >= since)
	ranked = query.as_("ranked")
	return (frappe.qb.from_(ranked).join(log).on(log.name == ranked.name)
		.select(*(getattr(log, field) for field in fields))
		.where(ranked.row_num == 1).run(as_dict=True))


def attach_sync_errors(rows):
	"""Add safe sync status and latest failed MES attempt for authorized, readable orders."""
	names = {row["name"] for row in rows if row.get("process_status") == PENDING}
	if not names or not has_release_permission() or not all(frappe.db.exists("DocType", doctype) for doctype in ("CRM Integration Log", "MES Integration Log")):
		return
	audits = latest_order_logs("CRM Integration Log", AUDIT_EVENT, names, ("reference_name", "creation", "status"))
	by_name = {row["name"]: row for row in rows if row["name"] in names}
	for audit in audits:
		if audit.status in {"Pending", "Success", "Failed"}:
			by_name[audit.reference_name]["sync_status"] = audit.status
	failed = {audit.reference_name: audit for audit in audits if audit.status == "Failed"}
	if not failed:
		return
	logs = latest_order_logs("MES Integration Log", "Sales Order Push To MES", failed,
		("reference_name", "creation", "status", "response_payload", "http_status_code"),
		since=min(audit.creation for audit in failed.values()))
	for log in logs:
		row = by_name.get(log.reference_name)
		if row and log.creation >= failed[log.reference_name].creation and log.status == "Failed":
			row["sync_error"] = mes_push_failure_summary(log)


def review_order(doc):
	data = {"name": doc.name, "process_status": doc.get("custom_process_status"), "can_release": False, "can_reject": False, "reason": "", "reject_reason": "", "sync_error": None, "sync_status": None}
	fields = readable_fields("Sales Order")
	for field in ("company", "custom_crm_order_no", "customer_name", "currency", "grand_total", "advance_paid", "party_account_currency"):
		if field in fields:
			data[field] = doc.get(field)
	data.update(recorded_receipts(doc))
	crm_enabled = is_crm_integration_enabled(doc.company)
	data["reject_reason"] = reject_reason(doc, crm_enabled)
	data["can_reject"] = not data["reject_reason"]
	data["last_confirmation"] = None
	if (frappe.db.exists("DocType", "CRM Integration Log") and frappe.has_permission("CRM Integration Log", "read")
		and {"user", "creation", "status"} <= readable_fields("CRM Integration Log")):
		logs = frappe.get_list("CRM Integration Log", filters={"event": AUDIT_EVENT, "reference_doctype": "Sales Order", "reference_name": doc.name},
			fields=["user", "creation", "status"], order_by="creation desc", limit_page_length=1)
		data["last_confirmation"] = logs[0] if logs else None
	if not has_release_permission():
		data["reason"] = _("未获得生产放行权限；管理员可在角色权限管理中配置。")
	elif not crm_enabled:
		data["reason"] = _("该公司未启用 CRM 集成。")
	elif doc.docstatus != 1 or doc.get("status") in {"Closed", "Cancelled", "Stopped", "On Hold"}:
		data["reason"] = _("仅可放行已提交且未关闭、未取消、未暂停的订单。")
	elif doc.get("custom_process_status") == PROCESSING:
		data["reason"] = _("CRM/MES 同步处理中，请勿重复确认。")
	elif doc.get("custom_process_status") != PENDING:
		data["reason"] = _("本单当前不处于待财务放行状态。")
	elif data["association_error"]:
		data["reason"] = _("收款与订单的公司或客户关联异常，请先核对原生单据。")
	else:
		data["can_release"] = True
	return data


def order_names(value):
	value = json.loads(value) if isinstance(value, str) else value
	if not isinstance(value, list) or not value or len(value) > 100 or any(not isinstance(name, str) or not name for name in value):
		frappe.throw(_("请选择 1 至 100 张订单。"))
	return list(dict.fromkeys(value))


@frappe.whitelist()
def get_finance_release_review(sales_orders):
	rows = []
	for name in order_names(sales_orders):
		try:
			rows.append(review_order(read_order(name)))
		except (frappe.PermissionError, frappe.DoesNotExistError):
			reason = _("订单不存在或不在当前可见范围内。")
			rows.append({"name": name, "can_release": False, "can_reject": False, "reason": reason, "reject_reason": reason})
	attach_sync_errors(rows)
	return {"orders": rows, "manual_confirmation": True}


@frappe.whitelist(methods=["POST"])
def confirm_production_release_batch(sales_orders):
	"""One transaction per order; reuse the original confirmation entry point and after-commit job."""
	from crm_integration.crm_integration.sales_order import confirm_deposit_and_push_to_mes

	if not has_release_permission():
		frappe.throw(_("未获得生产放行权限；管理员可在角色权限管理中配置。"), frappe.PermissionError)
	results = []
	for name in order_names(sales_orders):
		try:
			result = confirm_deposit_and_push_to_mes(name)
			frappe.db.commit()
			results.append({"name": name, "state": "processing", **result})
		except Exception:
			frappe.db.rollback()
			results.append({"name": name, "state": "failed", "message": _("本单未完成确认，请重新核验后重试。")})
	return {"orders": results}


def is_production_released(doc):
	return doc.docstatus == 1 and doc.get("status") not in {"Closed", "Cancelled", "Stopped", "On Hold"} and doc.get("custom_process_status") in RELEASED_STATES


def assert_production_released(name, company=None):
	doc = read_order(name)
	if company and doc.company != company:
		frappe.throw(_("生产单据与销售订单的公司不一致。"))
	if not is_crm_integration_enabled(doc.company):
		return
	if not is_production_released(doc):
		frappe.throw(_("销售订单尚未完成财务放行及 CRM/MES 同步，不能安排生产。"))


def validate_production_sources(doc, method=None):
	names = {doc.get("sales_order")} if doc.doctype == "Work Order" else {
		row.get("sales_order") for table in ("sales_orders", "po_items", "prod_plan_references", "sub_assembly_items") for row in doc.get(table) or []}
	for name in sorted(names - {None, ""}):
		assert_production_released(name, doc.get("company"))


def production_order_condition(sales_order_table):
	"""Use the same release states in existing production selectors and document validation."""
	from pypika.terms import ExistsCriterion
	company = frappe.qb.DocType("Company")
	crm_company = frappe.qb.from_(company).select(company.name).where(
		(company.name == sales_order_table.company) & (company.custom_enable_crm_integration == 1))
	return ~ExistsCriterion(crm_company) | sales_order_table.custom_process_status.isin(sorted(RELEASED_STATES))


def update_finance_audit(name, order_name, status, process_status):
	# Financial audit is mandatory; the general integration logger is intentionally best-effort.
	if frappe.db.get_value("CRM Integration Log", name, ["event", "reference_name"]) != (AUDIT_EVENT, order_name):
		frappe.throw(_("财务确认记录与订单不一致。"))
	frappe.db.set_value("CRM Integration Log", name, {"status": status, "response_payload": frappe.as_json({"process_status": process_status})})


def protect_process_status(doc, method=None):
	"""Read-only form fields are not an authorization boundary for native REST writes."""
	if not is_crm_integration_enabled(doc.get("company")):
		return
	previous = doc.get_doc_before_save()
	if previous and previous.get("custom_process_status") == doc.get("custom_process_status"):
		return
	if not previous and doc.get("custom_process_status") in {None, "", "Pending Confirmation"}:
		return
	frappe.throw(_("业务状态由财务确认和 CRM/MES 同步流程更新，不能直接修改。"))
