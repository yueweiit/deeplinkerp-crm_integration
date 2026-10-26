"""CRM order revision protocol; all writes are serialized by CRM order number."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, flt

from crm_integration.crm_integration.auth import validate_crm_api_user
from crm_integration.crm_integration.sales_order import (
	DEPOSIT_CONFIRMATION_PROCESSING,
	PENDING_CONFIRMATION,
	PENDING_DEPOSIT_CONFIRMATION,
	PENDING_PRODUCTION,
	REJECTED,
)
from crm_integration.crm_integration.sales_order_identity import crm_sales_order_creation_lock
from crm_integration.crm_integration.settings import is_crm_integration_enabled, throw_crm_integration_disabled


EARLY_STATUSES = {PENDING_CONFIRMATION, PENDING_DEPOSIT_CONFIRMATION}
EDIT_STATUSES = EARLY_STATUSES | {PENDING_PRODUCTION}
HEADER_FIELDS = {
	"custom_odt", "custom_remark", "delivery_date", "po_no", "po_date",
	"set_warehouse", "shipping_address_name", "shipping_address", "contact_person",
	"contact_display", "contact_mobile", "contact_email", "customer_address",
	"address_display", "payment_terms_template", "tc_name", "terms", "currency", "conversion_rate",
	"sales_team",
}
ITEM_FIELDS = {
	"qty", "rate", "custom_version", "custom_product", "custom_specifications",
	"description", "warehouse", "delivery_date", "uom", "conversion_factor",
	"discount_percentage", "discount_amount", "item_tax_template", "custom_item_tax_amount",
	"custom_mes_route_scenario",
}
STRUCTURAL_ITEM_FIELDS = {
	"qty", "item_code", "custom_version", "warehouse", "uom", "conversion_factor",
	"custom_mes_route_scenario",
}
STOCK_STRUCTURAL_ITEM_FIELDS = STRUCTURAL_ITEM_FIELDS - {"custom_mes_route_scenario"}


class CRMOrderConflict(frappe.ValidationError):
	http_status_code = 409


def parse_payload(value=None):
	if isinstance(value, str):
		value = frappe.parse_json(value)
	if value is None:
		if getattr(frappe, "request", None) and frappe.request.is_json:
			value = frappe.request.get_json(silent=True)
		else:
			value = dict(frappe.form_dict)
	if isinstance(value, dict) and isinstance(value.get("request"), dict):
		value = value["request"]
	if not isinstance(value, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))
	return value


def normalize_revision_date(value, fieldname):
	if value is None or value == "":
		return value
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
		try:
			return date.fromisoformat(value)
		except ValueError:
			pass
	frappe.throw(_("{0} 必须是有效的 YYYY-MM-DD 日期。").format(fieldname))


def order_names(crm_order_no):
	return frappe.get_all(
		"Sales Order", filters={"custom_crm_order_no": crm_order_no},
		fields=["name", "docstatus", "modified", "custom_process_status", "custom_crm_order_id"],
		order_by="creation asc, name asc", limit_page_length=0,
	)


def current_order(crm_order_no, erp_order_id=None, crm_order_id=None):
	rows = order_names(crm_order_no)
	active = [row for row in rows if cint(row.docstatus) == 1]
	if len(active) > 1:
		raise CRMOrderConflict(_("CRM 订单号 {0} 对应多张已提交订单，需人工处理。").format(crm_order_no))
	if active and erp_order_id and active[0].name != erp_order_id:
		raise CRMOrderConflict(_("ERP 单号与当前有效 CRM 订单不匹配。"))
	if active and crm_order_id and active[0].custom_crm_order_id and active[0].custom_crm_order_id != crm_order_id:
		raise CRMOrderConflict(_("CRM 稳定订单 ID 与当前有效订单不匹配。"))
	return (frappe.get_doc("Sales Order", active[0].name) if active else None), rows


def linked_facts(doc):
	if not doc:
		return {}
	name = doc.name
	delivery_notes = frappe.get_all(
		"Delivery Note Item", filters={"against_sales_order": name, "docstatus": 1},
		pluck="parent", limit_page_length=20,
	)
	work_orders = frappe.get_all(
		"Work Order", filters={"sales_order": name, "docstatus": ["!=", 2]},
		pluck="name", limit_page_length=20,
	)
	production_plans = frappe.get_all(
		"Production Plan Sales Order", filters={"sales_order": name, "docstatus": ["!=", 2]},
		pluck="parent", limit_page_length=20,
	)
	purchase_orders = frappe.get_all(
		"Purchase Order Item", filters={"sales_order": name, "docstatus": ["!=", 2]},
		pluck="parent", limit_page_length=20,
	)
	if doc.items:
		purchase_orders.extend(frappe.get_all(
			"Purchase Order Item", filters={
				"sales_order_item": ["in", [row.name for row in doc.items]], "docstatus": ["!=", 2],
			}, pluck="parent", limit_page_length=20,
		))
	stock_entries = []
	if frappe.db.has_column("Stock Entry", "custom_sales_order"):
		stock_entries = frappe.get_all(
			"Stock Entry", filters={"custom_sales_order": name, "docstatus": ["!=", 2]},
			pluck="name", limit_page_length=20,
		)
	reservations = frappe.get_all(
		"Stock Reservation Entry", filters={"voucher_type": "Sales Order", "voucher_no": name, "docstatus": 1},
		pluck="name", limit_page_length=20,
	)
	payment_entries = frappe.get_all(
		"Payment Entry Reference", filters={"reference_doctype": "Sales Order", "reference_name": name, "docstatus": 1},
		pluck="parent", limit_page_length=20,
	)
	sales_invoices = frappe.get_all(
		"Sales Invoice Item", filters={"sales_order": name, "docstatus": 1},
		pluck="parent", limit_page_length=20,
	)
	return {
		"deliveryNotes": sorted(set(delivery_notes)),
		"workOrders": work_orders,
		"productionPlans": sorted(set(production_plans)),
		"purchaseOrders": sorted(set(purchase_orders)),
		"stockEntries": stock_entries,
		"stockReservations": reservations,
		"paymentEntries": sorted(set(payment_entries)),
		"salesInvoices": sorted(set(sales_invoices)),
		"advancePaid": flt(doc.get("advance_paid")),
		"mesPush": mes_push_state(doc),
	}


def eligibility(doc, facts):
	if not doc:
		return "NO_ACTIVE_ORDER", "当前没有已提交的有效 ERP 销售订单。"
	if not is_crm_integration_enabled(doc.company):
		return "INTEGRATION_DISABLED", "订单所属公司未启用 CRM 集成。"
	if doc.status == "Closed":
		return "CLOSED", "ERP 销售订单已关闭。"
	if doc.custom_process_status == DEPOSIT_CONFIRMATION_PROCESSING:
		return "SYNC_PROCESSING", "定金确认及 MES 下推正在处理中。"
	if facts["deliveryNotes"] or any(flt(row.delivered_qty) for row in doc.items):
		return "DELIVERED", "订单已经发生实际出库。"
	if (cint(doc.get("custom_crm_production_started"))
		or doc.get("custom_crm_production_milestone") == "REACHED"):
		return "PRODUCTION_STARTED", "ERP 已向 CRM 确认进入生产阶段，订单不能修订。"
	if facts.get("stockEntries"):
		return "PRODUCTION_STARTED", "订单已有库存移动，不能修订。"
	mes_push = facts.get("mesPush") or {}
	if doc.custom_process_status == PENDING_PRODUCTION or mes_push.get("lastPushStatus") == "Success":
		return "MES_PUSHED", "订单已成功推送 MES，不能修改订单。"
	if doc.custom_process_status not in EDIT_STATUSES:
		return "STAGE_LOCKED", "订单已进入禁止修订或撤回阶段。"
	if production_milestone(doc, facts)["reached"] is None:
		return "PRODUCTION_UNKNOWN", "ERP 无法确认订单尚未进入生产阶段，不能修订。"
	return None, None


def mes_push_state(doc):
	if not doc or not frappe.db.exists("DocType", "MES Integration Log"):
		return {"lastPushStatus": None, "lastPushLog": None, "mesOrderId": None}
	logs = frappe.get_all("MES Integration Log", filters={
		"reference_doctype": "Sales Order", "reference_name": doc.name,
		"event": "Sales Order Push To MES",
	}, fields=["name", "status"], order_by="creation desc", limit_page_length=1)
	return {"lastPushStatus": logs[0].status if logs else None,
		"lastPushLog": logs[0].name if logs else None, "mesOrderId": None}


def production_milestone(doc, facts):
	if not doc:
		return {"reached": None, "source": "UNKNOWN", "time": None}
	if cint(doc.get("custom_crm_production_started")) or doc.get("custom_crm_production_milestone") == "REACHED":
		return {"reached": True, "source": "ERP E3 acknowledged", "time": None}
	mes_push = facts.get("mesPush") or {}
	if doc.custom_process_status == PENDING_PRODUCTION or facts.get("stockEntries") or mes_push.get("lastPushStatus") == "Success":
		return {"reached": True, "source": "ERP process/MES state", "time": None}
	if (doc.custom_process_status in EARLY_STATUSES
		and doc.get("custom_crm_production_milestone") == "NOT_STARTED"
		and not mes_push.get("lastPushStatus")):
		return {"reached": False, "source": "ERP E3 not attempted", "time": None}
	return {"reached": None, "source": "UNKNOWN", "time": None}


def describe_order(doc, rows):
	facts = linked_facts(doc)
	mes_push = facts.get("mesPush") or {"lastPushStatus": None, "lastPushLog": None, "mesOrderId": None}
	mes_confirmed = bool(doc and (doc.custom_process_status == PENDING_PRODUCTION or facts["stockEntries"]
		or mes_push["lastPushStatus"] == "Success"))
	mes_linked = True if mes_confirmed else None if mes_push["lastPushStatus"] in {"Pending", "Failed"} else False
	code, reason = eligibility(doc, facts)
	can_revoke = code in (None, "MES_PUSHED", "PRODUCTION_STARTED", "PRODUCTION_UNKNOWN") and not (
		facts["workOrders"] or facts["productionPlans"] or facts["stockEntries"]
		or facts["advancePaid"] or facts["paymentEntries"] or facts["salesInvoices"]
	)
	return {
		"status": "success", "crmOrderNo": doc.custom_crm_order_no if doc else None,
		"crmOrderId": doc.get("custom_crm_order_id") if doc else None,
		"effectiveErpOrderId": doc.name if doc else None,
		"erpVersion": str(doc.modified) if doc else None,
		"acceptedCrmRevision": cint(doc.get("custom_crm_revision")) if doc else None,
		"crmApprovalRound": cint(doc.get("custom_crm_approval_round")) if doc else None,
		"docstatus": doc.docstatus if doc else None,
		"nativeStatus": doc.status if doc else None,
		"processStatus": doc.custom_process_status if doc else None,
		"currency": doc.currency if doc else None,
		"conversionRate": flt(doc.conversion_rate) if doc else None,
		"taxes": [
			{"charge_type": row.charge_type, "account_head": row.account_head,
			 "tax_amount": row.tax_amount, "description": row.description}
			for row in (doc.get("taxes") if doc else []) or []
		],
		"salesTeam": [
			{"sales_person": row.sales_person, "allocated_percentage": row.allocated_percentage,
			 "allocated_amount": row.allocated_amount}
			for row in (doc.get("sales_team") if doc else []) or []
		],
		"history": [{"erpOrderId": row.name, "docstatus": row.docstatus, "active": row.docstatus == 1,
			"erpVersion": str(row.modified) if row.modified else None,
			"processStatus": row.custom_process_status} for row in rows],
		"items": [
			{"crmOrderItemId": row.get("custom_crm_order_item_id"), "erpOrderItemId": row.name,
			 "item_code": row.item_code, "custom_version": row.get("custom_version"),
			 "custom_item_tax_amount": row.get("custom_item_tax_amount"),
			 "custom_mes_route_scenario": row.get("custom_mes_route_scenario"),
			 "qty": row.qty, "uom": row.uom, "deliveredQty": row.delivered_qty,
			 "producedQty": row.get("produced_qty"), "productionQtySource": "ERP Sales Order Item"}
			for row in (doc.items if doc else [])
		],
		"productionMilestone": production_milestone(doc, facts),
		"associations": facts,
		"mesLinked": mes_linked,
		"mesPush": mes_push,
		"mesAction": "NOT_REQUESTED",
		"canEdit": not code,
		"canRevoke": bool(can_revoke),
		"reasonCode": code or ("ASSOCIATIONS_EXIST" if not can_revoke else None),
		"reason": reason or ("已有关联生产或定金，不能撤回。" if not can_revoke else None),
	}


def get_order_state(crm_order_no, crm_order_id=None, erp_order_id=None):
	validate_crm_api_user()
	if not crm_order_no:
		frappe.throw(_("缺少 crm_order_no。"))
	doc, rows = current_order(crm_order_no, erp_order_id, crm_order_id)
	result = describe_order(doc, rows)
	result["crmOrderNo"] = crm_order_no
	return result


def validate_operation(payload, expected_type):
	required = ["operationId", "crmOrderNo", "crmOrderId", "crmApprovalRound"]
	if expected_type != "ORDER_CREATE":
		required += ["erpOrderId", "expectedErpVersion"]
	for key in required:
		if not payload.get(key):
			frappe.throw(_("缺少必填字段：{0}").format(key))
	if len(str(payload["operationId"])) > 140:
		frappe.throw(_("operationId 不能超过 140 个字符。"))
	if cint(payload["crmApprovalRound"]) < 1:
		frappe.throw(_("crmApprovalRound 必须为正整数。"))
	if payload.get("operationType") != expected_type:
		frappe.throw(_("operationType 必须为 {0}。").format(expected_type))
	if not isinstance(payload.get("operator"), dict) or not payload["operator"].get("id"):
		frappe.throw(_("operator.id 为必填。"))
	if not payload.get("reason"):
		frappe.throw(_("reason 为必填。"))
	if expected_type == "ORDER_UPDATE":
		if payload.get("expectedCrmRevision") is None or payload.get("targetCrmRevision") is None:
			frappe.throw(_("修订必须提供 expectedCrmRevision 和 targetCrmRevision。"))
		if cint(payload["targetCrmRevision"]) != cint(payload["expectedCrmRevision"]) + 1:
			frappe.throw(_("CRM 目标版本必须比预期版本增加 1。"))
		if cint(payload["expectedCrmRevision"]) < 0:
			frappe.throw(_("CRM 内容版本不能为负数。"))
		if not payload.get("header") and not payload.get("items") and "taxes" not in payload:
			frappe.throw(_("修订请求必须包含 header、items 或 taxes。"))
	if expected_type == "ORDER_RESUBMIT":
		if not isinstance(payload.get("salesOrder"), dict):
			frappe.throw(_("重新提交必须包含 salesOrder 对象。"))
		if payload.get("targetCrmRevision") is None:
			frappe.throw(_("重新提交必须提供 targetCrmRevision。"))
	if expected_type == "ORDER_CREATE":
		if not isinstance(payload.get("salesOrder"), dict) or payload.get("targetCrmRevision") is None:
			frappe.throw(_("首次建单必须提供 salesOrder 和 targetCrmRevision。"))
	if expected_type in {"ORDER_CREATE", "ORDER_RESUBMIT"} and cint(payload["targetCrmRevision"]) < 1:
		frappe.throw(_("targetCrmRevision 必须为正整数。"))


def check_version(doc, payload, require_same_approval_round=True):
	if not doc:
		raise CRMOrderConflict(_("当前没有有效订单。"))
	if str(doc.modified) != str(payload["expectedErpVersion"]):
		raise CRMOrderConflict(_("ERP 版本已改变，请重新查询订单。"))
	if require_same_approval_round and cint(doc.get("custom_crm_approval_round")) not in (0, cint(payload["crmApprovalRound"])):
		raise CRMOrderConflict(_("CRM 审批轮次不匹配。"))
	if doc.get("custom_crm_order_id") and doc.custom_crm_order_id != payload["crmOrderId"]:
		raise CRMOrderConflict(_("CRM 稳定订单 ID 不匹配。"))
	if not is_crm_integration_enabled(doc.company):
		throw_crm_integration_disabled(doc.company)


def lock_expected_order(payload):
	modified = frappe.db.get_value("Sales Order", payload["erpOrderId"], "modified", for_update=True)
	if not modified or str(modified) != str(payload["expectedErpVersion"]):
		raise CRMOrderConflict(_("ERP 订单不存在或版本已改变，请重新查询。"))


def assert_editable(doc, facts):
	code, reason = eligibility(doc, facts)
	if code:
		raise CRMOrderConflict(_("{0}: {1}").format(code, reason))


def assert_revokeable(doc, facts):
	code, reason = eligibility(doc, facts)
	if code not in (None, "MES_PUSHED", "PRODUCTION_STARTED", "PRODUCTION_UNKNOWN"):
		raise CRMOrderConflict(_("{0}: {1}").format(code, reason))


def validate_sales_team_revision(rows):
	if not isinstance(rows, list):
		frappe.throw(_("sales_team 必须是数组。"))
	seen = set()
	total = 0.0
	precision = frappe.get_precision("Sales Team", "allocated_percentage") if rows else None
	rounding_method = frappe.get_system_settings("rounding_method") if rows else None
	for row in rows:
		if not isinstance(row, dict) or set(row) != {"sales_person", "allocated_percentage"}:
			frappe.throw(_("sales_team 每行只需 sales_person 和 allocated_percentage。"))
		person = row["sales_person"]
		if not isinstance(person, str) or not person or person != person.strip() or person in seen:
			frappe.throw(_("sales_team 销售人员为空或重复。"))
		seen.add(person)
		person_details = frappe.db.get_value(
			"Sales Person", person, ["enabled", "is_group"], as_dict=True
		)
		if not person_details or not cint(person_details.enabled):
			frappe.throw(_("销售人员 {0} 不存在或未启用。").format(person))
		if cint(person_details.is_group):
			frappe.throw(_("销售人员 {0} 是分组，不能加入销售团队。").format(person))
		if isinstance(row["allocated_percentage"], bool):
			frappe.throw(_("allocated_percentage 必须在 0 到 100 之间。"))
		try:
			percentage = Decimal(str(row["allocated_percentage"]))
		except (InvalidOperation, TypeError, ValueError):
			frappe.throw(_("allocated_percentage 必须在 0 到 100 之间。"))
		if not percentage.is_finite() or not 0 <= percentage <= 100:
			frappe.throw(_("allocated_percentage 必须在 0 到 100 之间。"))
		total += flt(percentage, precision, rounding_method=rounding_method)
	if rows and total != 100:
		frappe.throw(_("sales_team 的 allocated_percentage 按 ERP 精度舍入后合计必须为 100。"))


def apply_items(doc, changes, facts):
	from erpnext.controllers.accounts_controller import set_order_defaults
	from crm_integration.crm_integration.api import replace_sales_order_item, validate_crm_item_extra_fields

	if not isinstance(changes, list) or not changes:
		frappe.throw(_("items 必须是非空数组。"))
	seen = set()
	structural = False
	stock_structural = False
	by_name = {row.name: row for row in doc.items}
	by_crm_id = {row.custom_crm_order_item_id: row for row in doc.items if row.get("custom_crm_order_item_id")}
	if len(by_crm_id) != sum(bool(row.get("custom_crm_order_item_id")) for row in doc.items):
		raise CRMOrderConflict(_("当前订单存在重复 CRM 明细 ID，需人工处理。"))
	for change in changes:
		if not isinstance(change, dict) or change.get("action") not in {"ADD", "UPDATE", "DELETE"}:
			frappe.throw(_("每条明细必须指定 ADD、UPDATE 或 DELETE。"))
		action = change["action"]
		crm_id = change.get("crmOrderItemId")
		erp_id = change.get("erpOrderItemId")
		if not crm_id:
			frappe.throw(_("每条明细必须提供 crmOrderItemId。"))
		if crm_id in seen:
			frappe.throw(_("同一 CRM 明细不能在一次操作中重复。"))
		seen.add(crm_id)
		fields = set(change) - {"action", "crmOrderItemId", "erpOrderItemId"}
		if fields - ITEM_FIELDS - {"item_code"}:
			frappe.throw(_("存在不支持的明细字段：{0}。").format(", ".join(sorted(fields - ITEM_FIELDS - {"item_code"}))))
		validate_crm_item_extra_fields(change)
		if "delivery_date" in fields:
			change = {**change, "delivery_date": normalize_revision_date(change["delivery_date"], "items[].delivery_date")}
		if action == "ADD":
			if erp_id or crm_id in by_crm_id or not change.get("item_code") or flt(change.get("qty")) <= 0:
				raise CRMOrderConflict(_("新增明细的身份、物料或数量无效。"))
			if "rate" not in change or isinstance(change["rate"], bool):
				frappe.throw(_("新增明细必须提供有效的非负 rate。"))
			try:
				rate = Decimal(str(change["rate"]))
			except (InvalidOperation, TypeError, ValueError):
				frappe.throw(_("新增明细必须提供有效的非负 rate。"))
			if not rate.is_finite() or rate < 0:
				frappe.throw(_("新增明细必须提供有效的非负 rate。"))
			default_row = set_order_defaults("Sales Order", doc.name, "Sales Order Item", "items", change)
			defaults = {field: default_row.get(field) for field in (
				"item_code", "item_name", "description", "item_group", "delivery_date",
				"stock_uom", "uom", "warehouse", "conversion_factor", "item_tax_template", "item_tax_rate",
			)}
			defaults["delivery_date"] = normalize_revision_date(defaults["delivery_date"], "items[].delivery_date")
			row = doc.append("items", {**defaults, "custom_crm_order_item_id": crm_id})
			for field in fields:
				row.set(field, change[field])
			if "rate" in fields and flt(change["rate"]) == 0:
				row.price_list_rate = 0
				row.discount_amount = 0
				row.pricing_rules = None
			structural = True
			stock_structural = True
			continue
		row = by_name.get(erp_id) if erp_id else by_crm_id.get(crm_id)
		if not row or (erp_id and row.name != erp_id) or (row.get("custom_crm_order_item_id") and row.custom_crm_order_item_id != crm_id):
			raise CRMOrderConflict(_("CRM/ERP 明细身份不匹配。"))
		if crm_id in by_crm_id and by_crm_id[crm_id] is not row:
			raise CRMOrderConflict(_("CRM 明细 ID 已绑定其他 ERP 明细。"))
		if action == "DELETE":
			if fields or len(doc.items) <= 1:
				frappe.throw(_("删除明细不能携带业务字段，且不能删除最后一行。"))
			if any(flt(row.get(key)) for key in ("delivered_qty", "work_order_qty", "billed_amt", "produced_qty")):
				raise CRMOrderConflict(_("明细已有交付、生产或开票关联，不能删除。"))
			doc.items.remove(row)
			structural = True
			stock_structural = True
			continue
		if not fields:
			frappe.throw(_("UPDATE 明细至少需要一个变更字段。"))
		if "item_code" in fields and change["item_code"] != row.item_code:
			if any(flt(row.get(key)) for key in ("delivered_qty", "work_order_qty", "ordered_qty", "produced_qty", "billed_amt")):
				raise CRMOrderConflict(_("明细已有下游数量，不能替换物料。"))
			replace_sales_order_item(doc, row, change["item_code"])
		if "qty" in fields and flt(change["qty"]) <= 0:
			frappe.throw(_("qty 必须大于 0。"))
		if "qty" in fields and flt(change["qty"]) < flt(row.get("delivered_qty")):
			frappe.throw(_("qty 不得小于已交付数量。"))
		for field in fields - {"item_code"}:
			row.set(field, change[field])
		if "rate" in fields and flt(change["rate"]) == 0:
			row.price_list_rate = 0
			row.discount_amount = 0
			row.pricing_rules = None
		if not row.get("custom_crm_order_item_id"):
			row.custom_crm_order_item_id = crm_id
		structural |= bool(fields & STRUCTURAL_ITEM_FIELDS)
		stock_structural |= bool(fields & STOCK_STRUCTURAL_ITEM_FIELDS)
	if structural and (doc.custom_process_status not in EARLY_STATUSES or facts["workOrders"]
		or facts["productionPlans"] or facts["stockEntries"] or facts["stockReservations"]
		or (facts.get("mesPush") or {}).get("lastPushStatus")):
		raise CRMOrderConflict(_("STRUCTURAL_CHANGE_BLOCKED: 已推送 MES 或已有生产/库存预留关联，不能更改订单结构。"))
	return structural, stock_structural


def sync_header_delivery_date(doc, header, changes, previous_delivery_date):
	if "delivery_date" not in header or doc.order_type != "Sales" or doc.skip_delivery_note:
		return
	requested = header["delivery_date"]
	if not requested:
		frappe.throw(_("header.delivery_date 不能为空。"))
	if requested != previous_delivery_date:
		explicit_item_dates = {
			change["crmOrderItemId"] for change in changes or []
			if change["action"] != "DELETE" and "delivery_date" in change
		}
		for row in doc.items:
			if row.get("custom_crm_order_item_id") not in explicit_item_dates:
				row.delivery_date = requested
	dates = []
	for row in doc.items:
		if row.delivery_date:
			row.delivery_date = normalize_revision_date(row.delivery_date, "items[].delivery_date")
			dates.append(row.delivery_date)
	if dates and max(dates) != requested:
		raise CRMOrderConflict(_("header.delivery_date 必须等于修订后明细的最晚交期。"))


def validate_financial_revision(doc, header, taxes, facts):
	if not ({"currency", "conversion_rate"} & set(header)) and taxes is None:
		return
	if (facts["advancePaid"] or facts["paymentEntries"] or facts["salesInvoices"]
		or any(flt(row.get("billed_amt")) for row in doc.items)):
		raise CRMOrderConflict(_("订单已有收款或开票关联，不能修改币种、汇率或表头税费。"))
	if {"currency", "conversion_rate"} & set(header):
		currency = header.get("currency", doc.currency)
		if not isinstance(currency, str) or not currency:
			frappe.throw(_("currency 必须是有效币种代码。"))
		company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
		rate = header.get("conversion_rate", doc.conversion_rate)
		try:
			parsed_rate = Decimal(str(rate))
		except (InvalidOperation, TypeError, ValueError):
			parsed_rate = Decimal(0)
		if not parsed_rate.is_finite() or parsed_rate <= 0:
			frappe.throw(_("conversion_rate 必须大于 0。"))
		if currency == company_currency and parsed_rate != 1:
			frappe.throw(_("公司本位币的 conversion_rate 必须为 1。"))
		if currency != doc.currency and currency != company_currency and "conversion_rate" not in header:
			frappe.throw(_("切换为外币时必须提供 conversion_rate。"))
	if taxes is not None:
		if not isinstance(taxes, list):
			frappe.throw(_("taxes 必须是数组。"))
		allowed_fields = {"charge_type", "account_head", "tax_amount", "description",
			"cost_center", "included_in_print_rate", "add_deduct_tax", "category"}
		for tax in taxes:
			if not isinstance(tax, dict) or tax.get("charge_type") != "Actual" or not tax.get("account_head"):
				frappe.throw(_("表头税费只支持带 account_head 的 Actual 行。"))
			if set(tax) - allowed_fields or tax.get("tax_amount") is None:
				frappe.throw(_("Actual 税费行字段无效或缺少 tax_amount。"))
			try:
				amount = Decimal(str(tax["tax_amount"]))
			except (InvalidOperation, TypeError, ValueError):
				frappe.throw(_("tax_amount 必须是有效金额。"))
			if not amount.is_finite():
				frappe.throw(_("tax_amount 必须是有效金额。"))


def execute_update(payload):
	lock_expected_order(payload)
	doc, rows = current_order(payload["crmOrderNo"], payload["erpOrderId"], payload["crmOrderId"])
	check_version(doc, payload)
	facts = linked_facts(doc)
	assert_editable(doc, facts)
	if cint(doc.get("custom_crm_revision")) != cint(payload["expectedCrmRevision"]):
		raise CRMOrderConflict(_("CRM 内容版本不匹配。"))
	header = payload.get("header", {})
	if not isinstance(header, dict):
		frappe.throw(_("header 必须是对象。"))
	if set(header) - HEADER_FIELDS:
		frappe.throw(_("header 含有不支持的字段：{0}。").format(", ".join(sorted(set(header) - HEADER_FIELDS))))
	header = dict(header)
	for field in ("delivery_date", "po_date"):
		if field in header:
			header[field] = normalize_revision_date(header[field], f"header.{field}")
	if "taxes" in payload and payload["taxes"] is None:
		frappe.throw(_("taxes 必须是数组。"))
	taxes = payload.get("taxes") if "taxes" in payload else None
	validate_financial_revision(doc, header, taxes, facts)
	if "sales_team" in header:
		if facts["salesInvoices"]:
			raise CRMOrderConflict(_("订单已有销售发票，不能修改销售团队。"))
		validate_sales_team_revision(header["sales_team"])
	if set(header) & {"set_warehouse", "payment_terms_template"} and (
		doc.custom_process_status not in EARLY_STATUSES or facts["advancePaid"]
		or facts["paymentEntries"] or facts["salesInvoices"] or facts["stockReservations"]
	):
		raise CRMOrderConflict(_("仓库或付款条件已有下游关联，不能更改。"))
	if "set_warehouse" in header and (facts["workOrders"] or facts["productionPlans"] or facts["stockEntries"]):
		raise CRMOrderConflict(_("已有生产关联，不能更改仓库。"))
	if "set_warehouse" in header and (facts.get("mesPush") or {}).get("lastPushStatus"):
		raise CRMOrderConflict(_("MES 下推存在结果或结果未知，不能更改仓库。"))
	old_item_warehouses = {(row.item_code, row.warehouse) for row in doc.items if row.warehouse}
	old_delivery_date = (normalize_revision_date(doc.get("delivery_date"), "当前订单交期")
		if "delivery_date" in header else None)
	has_ordered_items = any(flt(row.get("ordered_qty")) for row in doc.items)
	old_item_amounts = {row.name: flt(row.get("amount")) for row in doc.items}
	old_payment_totals = (flt(doc.get("rounded_total") or doc.grand_total),
		flt(doc.get("base_rounded_total") or doc.base_grand_total))
	old_currency = doc.currency
	old_conversion_rate = flt(doc.conversion_rate)
	old_payment_template = doc.payment_terms_template
	for field, value in header.items():
		doc.set(field, value)
	if taxes is not None:
		doc.set("taxes_and_charges", None)
		doc.set("taxes", taxes)
	structural, stock_structural = apply_items(doc, payload["items"], facts) if payload.get("items") is not None else (False, False)
	sync_header_delivery_date(doc, header, payload.get("items"), old_delivery_date)
	if "set_warehouse" in header:
		for row in doc.items:
			row.warehouse = header["set_warehouse"]
		structural = True
		stock_structural = True
	if structural and (has_ordered_items or facts["purchaseOrders"]):
		raise CRMOrderConflict(_("订单已有采购关联，不能更改订单结构。"))
	if structural and (facts["advancePaid"] or facts["paymentEntries"] or facts["salesInvoices"]):
		raise CRMOrderConflict(_("已收定金订单的结构性修订需先完成财务处置。"))
	doc.custom_crm_order_id = payload["crmOrderId"]
	doc.custom_crm_approval_round = cint(payload["crmApprovalRound"])
	doc.custom_crm_revision = cint(payload["targetCrmRevision"])
	doc.flags.ignore_validate_update_after_submit = True
	doc.flags.crm_order_operation = True
	doc.ignore_pricing_rule = 1
	doc.set_missing_values()
	doc.calculate_taxes_and_totals()
	for row in doc.items:
		if (row.name in old_item_amounts and flt(row.get("amount")) != old_item_amounts[row.name]
			and flt(row.get("billed_amt")) > flt(row.get("amount"))):
			raise CRMOrderConflict(_("明细 {0} 的修订后金额低于已开票金额。").format(row.name))
	if stock_structural:
		from erpnext.stock.doctype.packed_item.packed_item import make_packing_list

		make_packing_list(doc)
	doc.set_gross_profit()
	if doc.payment_terms_template != old_payment_template:
		doc.set("payment_schedule", [])
	elif any(not flt(row.invoice_portion) for row in doc.get("payment_schedule") or []):
		new_payment_totals = (flt(doc.get("rounded_total") or doc.grand_total),
			flt(doc.get("base_rounded_total") or doc.base_grand_total))
		if (new_payment_totals != old_payment_totals or doc.currency != old_currency
			or flt(doc.conversion_rate) != old_conversion_rate):
			raise CRMOrderConflict(_("订单金额或币种发生变化，固定金额付款计划无法自动重算；请先更换付款条件。"))
	doc.set_payment_schedule()
	doc.validate_for_items()
	doc.validate_warehouse()
	doc.save(ignore_permissions=True)
	if stock_structural:
		from erpnext.stock.stock_balance import get_reserved_qty, update_bin_qty

		for item_code, warehouse in old_item_warehouses:
			update_bin_qty(item_code, warehouse, {"reserved_qty": get_reserved_qty(item_code, warehouse)})
		doc.update_reserved_qty()
		doc.update_prevdoc_status("submit")
		doc.update_delivery_status()
	doc.update_billing_percentage()
	doc.set_status()
	doc.reload()
	if ("delivery_date" in header and doc.order_type == "Sales" and not doc.skip_delivery_note
		and normalize_revision_date(doc.delivery_date, "订单交期") != header["delivery_date"]):
		raise CRMOrderConflict(_("ERP 保存后的表头交期与请求不一致，请核对明细交期。"))
	result = describe_order(doc, rows)
	result.update({"operationStatus": "SUCCEEDED", "mesAction": "NOT_REQUESTED",
		"itemMappings": [{"crmOrderItemId": row.get("custom_crm_order_item_id"), "erpOrderItemId": row.name} for row in doc.items],
		"grandTotal": doc.grand_total})
	return result


def execute_revoke(payload):
	lock_expected_order(payload)
	doc, rows = current_order(payload["crmOrderNo"], payload["erpOrderId"], payload["crmOrderId"])
	check_version(doc, payload)
	facts = linked_facts(doc)
	assert_revokeable(doc, facts)
	if (facts["workOrders"] or facts["productionPlans"]
		or facts["stockEntries"] or facts["advancePaid"] or facts["paymentEntries"] or facts["salesInvoices"]
	):
		raise CRMOrderConflict(_("订单已有定金或生产关联，不能撤回。"))
	if not doc.get("custom_crm_order_id") or not cint(doc.get("custom_crm_approval_round")):
		doc.custom_crm_order_id = payload["crmOrderId"]
		doc.custom_crm_approval_round = cint(payload["crmApprovalRound"])
		doc.flags.crm_order_operation = True
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.cancel()
	doc.reload()
	return {"status": "success", "operationStatus": "SUCCEEDED", "effectiveErpOrderId": None,
		"revokedErpOrderId": doc.name, "docstatus": doc.docstatus, "processStatus": doc.custom_process_status,
		"erpVersion": str(doc.modified), "mesAction": "NOT_REQUESTED", "associations": facts}


def execute_resubmit(payload):
	from crm_integration.crm_integration.api import create_sales_order, validate_sales_order_payload

	lock_expected_order(payload)
	current, history = current_order(payload["crmOrderNo"])
	if current:
		raise CRMOrderConflict(_("该 CRM 订单仍有有效 ERP 订单，不能重新提交。"))
	old = next((row for row in history if row.name == payload["erpOrderId"]), None)
	if not old or cint(old.docstatus) != 2:
		raise CRMOrderConflict(_("原 ERP 销售订单尚未取消，不能重新提交。"))
	# History is ordered by creation. A previous rejection must not start a new
	# branch after a later order has already been amended or cancelled.
	if old.name != history[-1].name:
		raise CRMOrderConflict(_("原 ERP 销售订单不是该 CRM 号的最新单据，请重新查询后重提。"))
	old_doc = frappe.get_doc("Sales Order", old.name)
	check_version(old_doc, payload, require_same_approval_round=False)
	# Financial rejection uses native cancellation and persists Rejected without
	# an R3 operation record. Ordinary manual cancellation remains ineligible.
	if old_doc.custom_process_status != REJECTED and not frappe.db.exists(
		"CRM Order Operation", {"operation_type": "ORDER_REVOKE", "status": "SUCCEEDED", "erp_order_id": old.name}
	):
		raise CRMOrderConflict(_("原 ERP 单未通过 CRM 审批撤回或财务驳回取消，不能自动重新提交。"))
	if cint(payload["crmApprovalRound"]) <= cint(old_doc.get("custom_crm_approval_round")):
		raise CRMOrderConflict(_("重新提交的 CRM 审批轮次必须大于原轮次。"))
	if cint(payload["targetCrmRevision"]) <= cint(old_doc.get("custom_crm_revision")):
		raise CRMOrderConflict(_("重新提交的 CRM 内容版本必须大于原版本。"))
	order = dict(payload["salesOrder"])
	if set(order) & {"name", "status", "custom_process_status", "amended_from", "owner", "modified_by"}:
		frappe.throw(_("重新提交报文包含 ERP 内部控制字段。"))
	validate_sales_order_payload(order)
	if order["custom_crm_order_no"] != payload["crmOrderNo"] or order["customer"] != old_doc.customer or order.get("company") != old_doc.company:
		raise CRMOrderConflict(_("重新提交的 CRM 号、公司或客户与历史订单不一致。"))
	if order.get("transaction_date") and str(order["transaction_date"]) != str(old_doc.transaction_date):
		raise CRMOrderConflict(_("重新提交不得更改原订单日期。"))
	order["transaction_date"] = old_doc.transaction_date
	order["custom_crm_order_id"] = payload["crmOrderId"]
	order["custom_crm_approval_round"] = cint(payload["crmApprovalRound"])
	order["custom_crm_revision"] = cint(payload["targetCrmRevision"])
	order["amended_from"] = old_doc.name
	item_ids = [row.get("custom_crm_order_item_id") for row in order["items"]]
	if not all(item_ids) or len(item_ids) != len(set(item_ids)):
		frappe.throw(_("重新提交的每条明细必须有唯一 custom_crm_order_item_id。"))
	response = create_sales_order(order, resubmission=True)
	new_doc = frappe.get_doc("Sales Order", response["name"])
	return {"status": "success", "operationStatus": "SUCCEEDED", "effectiveErpOrderId": new_doc.name,
		"previousErpOrderId": old_doc.name, "erpVersion": str(new_doc.modified), "docstatus": new_doc.docstatus,
		"acceptedCrmRevision": cint(new_doc.custom_crm_revision), "mesAction": "NOT_REQUESTED",
		"itemMappings": [{"crmOrderItemId": row.custom_crm_order_item_id, "erpOrderItemId": row.name} for row in new_doc.items]}


def execute_create(payload):
	from crm_integration.crm_integration.api import create_sales_order, validate_sales_order_payload

	if order_names(payload["crmOrderNo"]):
		raise CRMOrderConflict(_("该 CRM 订单号已有 ERP 历史单；请先查询订单状态，不要重复首次建单。"))
	order = dict(payload["salesOrder"])
	if set(order) & {"name", "status", "custom_process_status", "amended_from", "owner", "modified_by"}:
		frappe.throw(_("建单报文包含 ERP 内部控制字段。"))
	validate_sales_order_payload(order)
	if order["custom_crm_order_no"] != payload["crmOrderNo"] or not order.get("company"):
		raise CRMOrderConflict(_("salesOrder 的 CRM 号或公司无效。"))
	if not is_crm_integration_enabled(order["company"]):
		throw_crm_integration_disabled(order["company"])
	ids = [row.get("custom_crm_order_item_id") for row in order["items"]]
	if not all(ids) or len(ids) != len(set(ids)):
		frappe.throw(_("首次建单的每条明细必须有唯一 custom_crm_order_item_id。"))
	order["custom_crm_order_id"] = payload["crmOrderId"]
	order["custom_crm_approval_round"] = cint(payload["crmApprovalRound"])
	order["custom_crm_revision"] = cint(payload["targetCrmRevision"])
	response = create_sales_order(order)
	doc = frappe.get_doc("Sales Order", response["name"])
	return {"status": "success", "operationStatus": "SUCCEEDED", "effectiveErpOrderId": doc.name,
		"erpVersion": str(doc.modified), "acceptedCrmRevision": cint(doc.custom_crm_revision),
		"docstatus": doc.docstatus, "mesAction": "NOT_REQUESTED",
		"itemMappings": [{"crmOrderItemId": row.custom_crm_order_item_id, "erpOrderItemId": row.name} for row in doc.items]}


def run_operation(value, operation_type, executor):
	validate_crm_api_user()
	payload = parse_payload(value)
	validate_operation(payload, operation_type)
	business_payload = {key: value for key, value in payload.items()
		if key not in {"traceId", "retryOperator", "requestHash"}}
	canonical = json.dumps(business_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
	digest = hashlib.sha256(canonical.encode()).hexdigest()
	with crm_sales_order_creation_lock(payload["crmOrderNo"]):
		old = frappe.db.get_value("CRM Order Operation", payload["operationId"], ["request_hash", "result_payload", "status"])
		if old:
			if old[0] != digest:
				raise CRMOrderConflict(_("operationId 已用于另一份请求报文。"))
			if old[2] == "PROCESSING":
				return {"operationId": payload["operationId"], "operationStatus": "PROCESSING"}
			if old[2] != "FAILED":
				result = json.loads(old[1]) if old[1] else {"operationStatus": old[2]}
				result["idempotentReplay"] = True
				return result
			op = frappe.get_doc("CRM Order Operation", payload["operationId"])
			op.status = "PROCESSING"
			op.attempt_count = cint(op.attempt_count) + 1
			op.save(ignore_permissions=True)
		else:
			operator = payload["operator"]
			op = frappe.get_doc({"doctype": "CRM Order Operation", "operation_id": payload["operationId"],
				"operation_type": operation_type, "status": "PROCESSING", "attempt_count": 1,
				"crm_order_no": payload["crmOrderNo"], "crm_order_id": payload["crmOrderId"],
				"erp_order_id": payload.get("erpOrderId"), "request_hash": digest,
				"request_payload": canonical, "operator_id": operator["id"], "reason": payload["reason"]})
			op.insert(ignore_permissions=True)
		frappe.db.savepoint("crm_order_business")
		try:
			result = executor(payload)
			result["operationId"] = payload["operationId"]
			result["idempotentReplay"] = False
			op.status = "SUCCEEDED"
			op.erp_order_id = result.get("effectiveErpOrderId") or result.get("revokedErpOrderId")
		except Exception as exc:
			frappe.db.rollback(save_point="crm_order_business")
			op.status = "REJECTED" if isinstance(exc, (frappe.ValidationError, frappe.PermissionError)) else "FAILED"
			reason_code = (
			"PERMISSION_DENIED" if isinstance(exc, frappe.PermissionError) else
			"ORDER_CONFLICT" if isinstance(exc, CRMOrderConflict) else
			"VALIDATION_FAILED" if isinstance(exc, frappe.ValidationError) else "INTERNAL_ERROR"
		)
			result = {"operationId": payload["operationId"], "operationStatus": op.status,
				"status": "error", "reasonCode": reason_code, "message": str(exc),
				"sideEffects": False, "retryable": op.status == "FAILED",
				"idempotentReplay": False}
			op.error_message = str(exc)[:1000]
		op.result_payload = json.dumps(result, ensure_ascii=False, default=str)
		if op.status == "SUCCEEDED":
			op.error_message = None
		op.save(ignore_permissions=True)
		# The per-order lock must remain held until both the business change and result are durable.
		frappe.db.commit()
		return result


def revise_order(value=None):
	return run_operation(value, "ORDER_UPDATE", execute_update)


def create_order(value=None):
	return run_operation(value, "ORDER_CREATE", execute_create)


def revoke_order(value=None):
	return run_operation(value, "ORDER_REVOKE", execute_revoke)


def resubmit_order(value=None):
	return run_operation(value, "ORDER_RESUBMIT", execute_resubmit)


def get_operation_result(operation_id, crm_order_no=None):
	validate_crm_api_user()
	if not operation_id:
		frappe.throw(_("缺少 operation_id。"))
	row = frappe.db.get_value("CRM Order Operation", operation_id,
		["crm_order_no", "status", "request_hash", "result_payload", "operation_type"], as_dict=True)
	if row and crm_order_no and row.crm_order_no != crm_order_no:
		raise CRMOrderConflict(_("operationId 与 CRM 订单号不匹配。"))
	if not row:
		# An uncommitted request may still arrive; absence alone cannot prove NOT_EXECUTED.
		response = {"operationId": operation_id, "operationStatus": "UNKNOWN", "sideEffects": None,
			"retryable": False, "message": _("未找到持久操作记录，请核对订单当前状态后使用原 operationId 重试。")}
		if crm_order_no:
			try:
				current, history = current_order(crm_order_no)
				response["currentOrder"] = describe_order(current, history)
			except CRMOrderConflict as exc:
				response["currentOrderError"] = str(exc)
		return response
	result = json.loads(row.result_payload) if row.result_payload else {}
	response = {"operationId": operation_id, "operationType": row.operation_type,
		"operationStatus": row.status, "requestHash": row.request_hash,
		"crmOrderNo": row.crm_order_no, "result": result}
	try:
		current, history = current_order(row.crm_order_no)
		response["currentOrder"] = describe_order(current, history)
	except CRMOrderConflict as exc:
		response["currentOrderError"] = str(exc)
	return response
