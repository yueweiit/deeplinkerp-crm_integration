import hashlib

import frappe
import requests
from requests.exceptions import RequestException
from frappe import _
from frappe.utils import cint, flt, now
from frappe.utils.data import get_datetime

from crm_integration.crm_integration.integration_log import create_crm_log, update_crm_log
from crm_integration.crm_integration.sales_order_identity import crm_sales_order_creation_lock, get_sales_order_names_by_crm_order_no
from crm_integration.crm_integration.settings import is_crm_integration_enabled, throw_crm_integration_disabled


PENDING_CONFIRMATION = "Pending Confirmation"
REJECTED = "Rejected"
PENDING_DEPOSIT_CONFIRMATION = "Pending Deposit Confirmation"
DEPOSIT_CONFIRMATION_PROCESSING = "Deposit Confirmation Processing"
PENDING_PRODUCTION = "Pending Production"
PENDING_FINAL_PAYMENT = "Pending Final Payment"
DELIVERABLE = "Deliverable"
PARTIALLY_DELIVERED= "Partially Delivered"
CLOSED = "Closed"
CANCELLED = "Cancelled"

CRM_STATUS_API_TIMEOUT = 15
MES_SALES_ORDER_PUSH_EVENT = "sales_order.created"
CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION = "CONFIRMED_DEPOSIT_PUSH_PRODUCTION"
CRM_STATUS_IN_PRODUCTION = "IN_PRODUCTION"
CRM_STATUS_FINANCE_REJECTED = "FINANCE_REJECTED"
CRM_STATUS_SHIPMENT_CREATED = "SHIPMENT_CREATED"
CRM_STATUS_SHIPMENT_CANCELLED = "SHIPMENT_CANCELLED"
CRM_STATUS_PENDING_SHIPMENT = "PENDING_SHIPMENT"
CRM_STATUS_PRODUCTION_PROGRESS_REPORTED = "PRODUCTION_PROGRESS_REPORTED"
CRM_TRACE_ID_MAX_LENGTH = 128

CRM_STATUS_EVENTS = {
	CRM_STATUS_FINANCE_REJECTED: "Sales Order Rejected",
	CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION: "Confirmed Deposit Push Production",
	CRM_STATUS_IN_PRODUCTION: "In Production",
	CRM_STATUS_SHIPMENT_CREATED: "Shipment Created",
	CRM_STATUS_SHIPMENT_CANCELLED: "Shipment Cancelled",
	CRM_STATUS_PENDING_SHIPMENT: "Final Payment Reconciled",
	CRM_STATUS_PRODUCTION_PROGRESS_REPORTED: "Production Progress Reported",
}

PRODUCT_SERIES_NAME_MAP = {
	"NBA": "亮甲2.0",
	"NDN": "ALTERNA",
	"NHA": "超级队长",
	"NFA": "骑士",
	"NFB": "骑士2.0",
	"NWV": "薇武士",
	"NBD": "圣殿",
}


def prevent_duplicate_crm_order_no(doc, method=None):
	crm_order_no = doc.get("custom_crm_order_no")
	if not crm_order_no:
		return

	existing_names = get_sales_order_names_by_crm_order_no(crm_order_no, limit=1)
	resubmission = bool((getattr(doc, "flags", None) or {}).get("crm_resubmission"))
	if existing_names and not resubmission:
		frappe.throw(
			_("CRM 销售订单号 {0} 已被销售订单 {1} 使用。").format(
				crm_order_no, existing_names[0]
			)
		)
	if resubmission:
		active = frappe.get_all("Sales Order", filters={
			"custom_crm_order_no": crm_order_no, "docstatus": ["!=", 2],
		}, pluck="name", limit_page_length=1)
		if active:
			frappe.throw(_("CRM 订单号 {0} 仍存在有效或草稿销售订单 {1}。").format(crm_order_no, active[0]))


def protect_crm_order_identity(doc, method=None):
	if doc.flags.get("crm_order_operation"):
		return
	previous = doc.get_doc_before_save()
	if not previous or not (previous.get("custom_crm_order_no") or doc.get("custom_crm_order_no")):
		return
	for field in ("custom_crm_order_no", "custom_crm_order_id", "custom_crm_approval_round",
		"custom_crm_revision", "custom_crm_production_started", "custom_crm_production_milestone"):
		if doc.get(field) != previous.get(field):
			frappe.throw(_("CRM 订单身份与版本只能通过受控接口修改：{0}。").format(field))
	previous_rows = {row.name: row for row in previous.get("items") or []}
	for row in doc.get("items") or []:
		old = previous_rows.get(row.name)
		if old and row.get("custom_crm_order_item_id") != old.get("custom_crm_order_item_id"):
			frappe.throw(_("CRM 明细身份只能通过受控接口修改。"))
		if not old and row.get("custom_crm_order_item_id"):
			frappe.throw(_("新增 CRM 明细身份只能通过受控接口设置。"))


@frappe.whitelist()
def get_sales_order_list_details(sales_orders):
	if isinstance(sales_orders, str):
		sales_orders = frappe.parse_json(sales_orders)

	sales_orders = [name for name in (sales_orders or []) if name]
	if not sales_orders:
		return {}

	allowed_sales_orders = [
		name
		for name in sales_orders
		if frappe.has_permission("Sales Order", "read", doc=name)
	]
	if not allowed_sales_orders:
		return {}

	details = {
		name: {"sales_person": "", "product": ""}
		for name in allowed_sales_orders
	}
	set_first_sales_person(details, allowed_sales_orders)
	set_first_product(details, allowed_sales_orders)
	return details


def set_first_sales_person(details, sales_orders):
	seen = set()
	sales_team_rows = frappe.get_all(
		"Sales Team",
		filters={"parenttype": "Sales Order", "parent": ["in", sales_orders]},
		fields=["parent", "sales_person"],
		order_by="parent asc, idx asc",
	)

	for row in sales_team_rows:
		if row.parent in seen:
			continue

		details[row.parent]["sales_person"] = row.sales_person or ""
		seen.add(row.parent)


def set_first_product(details, sales_orders):
	seen = set()
	item_rows = frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", sales_orders]},
		fields=["parent", "custom_product"],
		order_by="parent asc, idx asc",
	)

	for row in item_rows:
		if row.parent in seen:
			continue

		details[row.parent]["product"] = row.custom_product or ""
		seen.add(row.parent)


def set_process_status(sales_order, process_status, status=None):
	"""Update process status without running the full Sales Order save cycle."""
	if not is_crm_integration_enabled(sales_order.get("company")):
		return

	sales_order.db_set("custom_process_status", process_status, update_modified=True)
	if status:
		sales_order.db_set("status", status, update_modified=True)
	sales_order.notify_update()
	update_related_delivery_note_readiness_status(sales_order.name)


def update_related_delivery_note_readiness_status(sales_order_name):
	try:
		from mes_integration.mes_integration.delivery_note import (
			update_delivery_readiness_status_for_sales_orders,
		)

		update_delivery_readiness_status_for_sales_orders([sales_order_name])
	except ModuleNotFoundError as exc:
		if is_missing_mes_integration_error(exc):
			return
		raise
	except Exception:
		frappe.log_error(
			title="Failed to update Delivery Note readiness status",
			message=frappe.get_traceback(),
		)


def assert_sales_order_not_closed(sales_order):
	if sales_order.get("status") == CLOSED:
		frappe.throw(_("已关闭的销售订单不能执行生产流程操作，请先重新打开订单。"))


def get_crm_status_api_url():
	url = frappe.conf.get("crm_status_api_url")
	if not url:
		frappe.throw(_("缺少 CRM 状态推送接口配置：crm_status_api_url"))
	return url


def get_crm_status_api_key():
	api_key = frappe.conf.get("crm_status_api_key")
	if not api_key:
		frappe.throw(_("缺少 CRM 状态推送接口配置：crm_status_api_key"))
	return api_key


def get_crm_status_api_verify_ssl():
	return cint(frappe.conf.get("crm_status_api_verify_ssl", 1)) == 1


def get_crm_status_event(external_status):
	return CRM_STATUS_EVENTS.get(external_status) or "Sales Order Status Push"


def push_sales_order_status_to_crm(
	sales_order, external_status, remark=None, production_batch_no=None
):
	if not is_crm_integration_enabled(sales_order.get("company")):
		throw_crm_integration_disabled(sales_order.get("company"))

	external_order_id = sales_order.get("custom_crm_order_no")
	if not external_order_id:
		frappe.throw(_("缺少 CRM 订单编号，无法推送状态到 CRM。"))

	trace_id = (
		make_production_progress_trace_id(external_order_id, production_batch_no)
		if external_status == CRM_STATUS_PRODUCTION_PROGRESS_REPORTED
		else make_crm_trace_id(sales_order.name, external_status)
	)
	payload = {
		"sourceSystem": "ERP",
		"externalStatus": external_status,
		"externalOrderId": external_order_id,
		"erpOrderId": sales_order.name,
		"crmApprovalRound": cint(sales_order.get("custom_crm_approval_round")),
		"remark": remark if remark is not None else sales_order.get("custom_remark") or "",
		"traceId": trace_id,
		"attachments": [],
	}
	request_url = get_crm_status_api_url()
	headers = {
		"Content-Type": "application/json",
		"X-API-Key": get_crm_status_api_key(),
	}
	crm_log = create_crm_log(
		direction="Outbound",
		event=get_crm_status_event(external_status),
		status="Pending",
		reference_doctype="Sales Order",
		reference_name=sales_order.name,
		source="ERPNext",
		request_url=request_url,
		request_payload=payload,
		trace_id=payload["traceId"],
		external_status=external_status,
	)

	try:
		response = requests.post(
			request_url,
			json=payload,
			headers=headers,
			timeout=CRM_STATUS_API_TIMEOUT,
			verify=get_crm_status_api_verify_ssl(),
		)
		response_payload = parse_crm_response(response)
		response.raise_for_status()
	except requests.RequestException as exc:
		response = getattr(exc, "response", None)
		update_crm_log(
			crm_log,
			status="Failed",
			response_payload=parse_crm_response(response),
			error_message=frappe.get_traceback(),
			http_status_code=getattr(response, "status_code", None),
		)
		frappe.log_error(
			title=_("CRM 状态推送失败"),
			message=frappe.get_traceback(),
		)
		frappe.throw(_("CRM 状态推送失败：{0}").format(str(exc)))

	update_crm_log(
		crm_log,
		status="Success",
		response_payload=response_payload,
		http_status_code=response.status_code,
	)
	frappe.logger().info(
		f"Pushed Sales Order {sales_order.name} status {external_status} to CRM with traceId {payload['traceId']}"
	)
	return response_payload


def enqueue_sales_order_status_to_crm(
	sales_order_name,
	external_status,
	triggered_status=None,
	trigger_event=None,
	delivery_note_name=None,
	items=None,
	remark=None,
	production_batch_no=None,
):
	job_args = {
		"sales_order_name": sales_order_name,
		"external_status": external_status,
		"triggered_status": triggered_status,
		"trigger_event": trigger_event,
		"delivery_note_name": delivery_note_name,
		"items": items or [],
		"remark": remark,
	}
	if production_batch_no is not None:
		job_args["production_batch_no"] = production_batch_no

	frappe.enqueue(
		"crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm_job",
		queue="short",
		enqueue_after_commit=True,
		**job_args,
	)


def push_sales_order_status_to_crm_job(
	sales_order_name,
	external_status,
	triggered_status=None,
	trigger_event=None,
	delivery_note_name=None,
	items=None,
	remark=None,
	production_batch_no=None,
):
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	if not is_crm_integration_enabled(sales_order.get("company")):
		return None

	return push_sales_order_status_payload_to_crm(
		sales_order=sales_order,
		external_status=external_status,
		triggered_status=triggered_status,
		trigger_event=trigger_event,
		delivery_note_name=delivery_note_name,
		items=items or [],
		remark=remark,
		production_batch_no=production_batch_no,
	)


def push_sales_order_status_payload_to_crm(
	sales_order,
	external_status,
	triggered_status=None,
	trigger_event=None,
	delivery_note_name=None,
	items=None,
	remark=None,
	production_batch_no=None,
):
	external_order_id = sales_order.get("custom_crm_order_no")
	if external_status == CRM_STATUS_PRODUCTION_PROGRESS_REPORTED:
		trace_id = make_production_progress_trace_id(
			external_order_id,
			production_batch_no or delivery_note_name,
		)
	else:
		trace_id = make_crm_trace_id(
			sales_order.name,
			"-".join(filter(None, [external_status, delivery_note_name])),
		)
	payload = {
		"sourceSystem": "ERP",
		"externalStatus": external_status,
		"externalOrderId": external_order_id,
		"erpOrderId": sales_order.name,
		"crmApprovalRound": cint(sales_order.get("custom_crm_approval_round")),
		"remark": remark
		if remark is not None
		else get_crm_status_remark(sales_order, triggered_status, delivery_note_name),
		"traceId": trace_id,
		"attachments": [],
	}

	items = items or []
	if items:
		payload["items"] = items

	if trigger_event:
		payload["triggerEvent"] = trigger_event
		payload["trigger_event"] = trigger_event

	if delivery_note_name:
		payload["deliveryNoteName"] = delivery_note_name
		payload["delivery_note"] = delivery_note_name

	crm_log = create_crm_log(
		direction="Outbound",
		event=get_crm_status_event(external_status),
		status="Pending",
		reference_doctype="Sales Order",
		reference_name=sales_order.name,
		source="ERPNext",
		request_payload=payload,
		trace_id=trace_id,
		external_status=external_status,
	)

	if not external_order_id:
		error_message = _("缺少 CRM 订单编号，无法推送状态到 CRM。")
		update_crm_log(crm_log, status="Failed", error_message=error_message)
		frappe.throw(error_message)

	response = None
	try:
		request_url = get_crm_status_api_url()
		update_crm_log(crm_log, request_url=request_url)
		headers = {
			"Content-Type": "application/json",
			"X-API-Key": get_crm_status_api_key(),
		}
		response = requests.post(
			request_url,
			json=payload,
			headers=headers,
			timeout=CRM_STATUS_API_TIMEOUT,
			verify=get_crm_status_api_verify_ssl(),
		)
		response_payload = parse_crm_response(response)
		response.raise_for_status()
	except Exception:
		update_crm_log(
			crm_log,
			status="Failed",
			response_payload=parse_crm_response(response),
			error_message=frappe.get_traceback(),
			http_status_code=getattr(response, "status_code", None),
		)
		frappe.log_error(
			title=_("CRM 状态推送失败"),
			message=frappe.get_traceback(),
		)
		raise

	update_crm_log(
		crm_log,
		status="Success",
		response_payload=response_payload,
		http_status_code=response.status_code,
	)
	frappe.logger().info(
		f"Pushed Sales Order {sales_order.name} status {external_status} to CRM with traceId {trace_id}"
	)
	return response_payload


def get_crm_status_remark(sales_order, triggered_status=None, delivery_note_name=None):
	process_status = triggered_status or sales_order.get("custom_process_status") or ""
	if delivery_note_name:
		return _("销售出库 {0} 已提交，ERP流程状态：{1}").format(
			delivery_note_name, process_status
		)

	return sales_order.get("custom_remark") or ""


def make_crm_trace_id(sales_order_name, external_status):
	"""Build a stable idempotency key for one Sales Order status event."""
	trace_id = f"erp-{sales_order_name}-{external_status}"
	if len(trace_id) <= CRM_TRACE_ID_MAX_LENGTH:
		return trace_id

	digest = hashlib.sha256(trace_id.encode()).hexdigest()
	return f"erp-sales-order-{digest}"


def make_production_progress_trace_id(external_order_id, production_batch_no):
	"""Build the stable idempotency key for one MES production batch."""
	external_order_id = str(external_order_id or "").strip()
	production_batch_no = str(production_batch_no or "").strip()
	if not external_order_id or not production_batch_no:
		frappe.throw(_("生产进度回传缺少 CRM 订单号或生产批次号，无法生成 traceId。"))

	trace_id = f"MES:PRODUCTION:{external_order_id}:{production_batch_no}"
	if len(trace_id) > CRM_TRACE_ID_MAX_LENGTH:
		frappe.throw(_("生产进度回传 traceId 不能超过 {0} 个字符。").format(CRM_TRACE_ID_MAX_LENGTH))

	return trace_id


def parse_crm_response(response):
	if response is None:
		return None

	content_type = response.headers.get("content-type") or ""
	if content_type.startswith("application/json"):
		try:
			return response.json()
		except ValueError:
			return response.text
	return response.text


def get_mes_sales_order_push_url():
	url = frappe.conf.get("mes_sales_order_push_url")
	if not url:
		frappe.throw(_("缺少 MES 销售订单推送接口配置：mes_sales_order_push_url"))
	return url


def get_mes_sales_order_push_authorization():
	authorization = frappe.conf.get("mes_push_authorization")
	if not authorization:
		frappe.throw(_("缺少 MES 推送接口配置：mes_push_authorization"))
	return authorization


def is_missing_mes_integration_error(exc):
	return bool(exc.name and exc.name.startswith("mes_integration"))


def get_mes_integration_services():
	try:
		from mes_integration.mes_integration.integration_log import create_mes_log, update_mes_log
		from mes_integration.mes_integration.settings import (
			is_mes_integration_enabled,
			throw_mes_integration_disabled,
		)
	except ModuleNotFoundError as exc:
		if not is_missing_mes_integration_error(exc):
			raise
		frappe.throw(_("推送销售订单到 MES 需要安装 mes_integration 应用。"))

	return frappe._dict(
		create_log=create_mes_log,
		update_log=update_mes_log,
		is_enabled=is_mes_integration_enabled,
		throw_disabled=throw_mes_integration_disabled,
	)


def validate_mes_sync_available(sales_order):
	services = get_mes_integration_services()
	if not services.is_enabled(sales_order.get("company")):
		services.throw_disabled(sales_order.get("company"))
	return services


def push_sales_order_to_mes(sales_order):
	services = validate_mes_sync_available(sales_order)

	payload = None
	request_url = None
	mes_log = None
	response = None
	response_payload = None

	try:
		payload = build_mes_sales_order_payload(sales_order)
		request_url = get_mes_sales_order_push_url()
		headers = {
			"Content-Type": "application/json",
			"Authorization": get_mes_sales_order_push_authorization(),
		}
		mes_log = services.create_log(
			direction="Outbound",
			event="Sales Order Push To MES",
			status="Pending",
			reference_doctype="Sales Order",
			reference_name=sales_order.name,
			source="ERPNext",
			request_url=request_url,
			request_payload=payload,
		)
		response = requests.post(
			request_url,
			json=payload,
			headers=headers,
			timeout=flt(frappe.conf.get("mes_request_timeout") or 15),
		)
		response_payload = parse_crm_response(response)
		response.raise_for_status()
		validate_mes_sales_order_response(response_payload)
	except RequestException as exc:
		response = getattr(exc, "response", None)
		response_payload = parse_crm_response(response) if response is not None else response_payload
		log_failed_mes_sales_order_push(
			sales_order=sales_order,
			mes_log=mes_log,
			request_url=request_url,
			request_payload=payload,
			response_payload=response_payload,
			error_message=frappe.get_traceback(),
			http_status_code=getattr(response, "status_code", None),
			services=services,
		)
		frappe.log_error(title=_("MES 销售订单推送失败"), message=frappe.get_traceback())
		frappe.throw(_("MES 销售订单推送失败：{0}").format(str(exc)))
	except Exception:
		log_failed_mes_sales_order_push(
			sales_order=sales_order,
			mes_log=mes_log,
			request_url=request_url,
			request_payload=payload,
			response_payload=response_payload,
			error_message=frappe.get_traceback(),
			http_status_code=getattr(response, "status_code", None),
			services=services,
		)
		frappe.log_error(title=_("MES 销售订单推送失败"), message=frappe.get_traceback())
		raise

	services.update_log(
		mes_log,
		status="Success",
		response_payload=response_payload,
		http_status_code=response.status_code,
	)
	return response_payload


def log_failed_mes_sales_order_push(
	sales_order,
	mes_log=None,
	request_url=None,
	request_payload=None,
	response_payload=None,
	error_message=None,
	http_status_code=None,
	services=None,
):
	services = services or get_mes_integration_services()
	http_status_code = http_status_code if http_status_code is not None else 0
	log_values = {
		"status": "Failed",
		"response_payload": response_payload,
		"error_message": error_message,
		"http_status_code": http_status_code,
	}
	if mes_log:
		services.update_log(mes_log, **log_values)
	else:
		services.create_log(
			direction="Outbound",
			event="Sales Order Push To MES",
			status="Failed",
			reference_doctype="Sales Order",
			reference_name=sales_order.name,
			source="ERPNext",
			request_url=request_url,
			request_payload=request_payload,
			response_payload=response_payload,
			error_message=error_message,
			http_status_code=http_status_code,
		)

	frappe.db.commit()


def build_mes_sales_order_payload(sales_order):
	crm_order_no = sales_order.get("custom_crm_order_no")
	if not crm_order_no:
		frappe.throw(_("缺少 CRM 订单编号，无法推送销售订单到 MES。"))

	items = build_mes_sales_order_items(sales_order)
	if not items:
		frappe.throw(_("销售订单缺少可推送到 MES 的明细行。"))

	product_series_id = get_product_series_id(items)

	data = compact_dict({
		"name": sales_order.name,
		"customer_name": sales_order.get("customer"),
		"company": sales_order.get("company"),
		"transaction_date": format_date_value(sales_order.get("transaction_date")),
		"delivery_date": format_date_value(sales_order.get("delivery_date")),
		"status": sales_order.get("status"),
		"docstatus": 1,
		"total_qty": flt(sales_order.get("total_qty")),
		"total": flt(sales_order.get("total")),
		"grand_total": flt(sales_order.get("grand_total")),
		"owner": sales_order.get("owner"),
		"custom_odt": sales_order.get("custom_odt"),
		"custom_crm_order_no": sales_order.get("custom_crm_order_no"),
		"product_series_id": product_series_id,
		"product_name": get_product_series_name(product_series_id),
		"modified": get_datetime(sales_order.modified).isoformat() if sales_order.get("modified") else None,
		"items": items,
	})

	return compact_dict({
		"event": MES_SALES_ORDER_PUSH_EVENT,
		"doc_type": "Sales Order",
		"doc_name": sales_order.name,
		"data": data,
		"triggered_by": "erp_confirm_deposit",
		"timestamp": get_datetime().isoformat(),
	})


def build_mes_sales_order_items(sales_order):
	items = []
	for row in sales_order.get("items", []):
		if not row.get("item_code") or flt(row.get("qty")) <= 0:
			continue

		items.append(compact_dict({
			"name": row.get("name"),
			"idx": row.get("idx"),
			"item_code": row.get("item_code"),
			"item_name": row.get("item_name"),
			"description": row.get("description"),
			"color": row.get("custom_specifications"),
			"custom_version": row.get("custom_version"),
			"custom_mes_route_scenario": row.get("custom_mes_route_scenario"),
			"qty": flt(row.get("qty")),
			"uom": row.get("uom"),
			"rate": flt(row.get("rate")),
			"amount": flt(row.get("amount")),
			"delivery_date": format_date_value(row.get("delivery_date")),
			"warehouse": get_item_source_warehouse(row, sales_order),
		}))
	return items


def get_item_source_warehouse(row, sales_order):
	return row.get("warehouse") or sales_order.get("set_warehouse")


def get_product_series_id(items):
	for item in items:
		item_code = item.get("item_code")
		if item_code:
			return item_code[:7]
	return None




def get_product_series_name(product_series_id):
	if not product_series_id:
		return None
	return PRODUCT_SERIES_NAME_MAP.get(product_series_id[:3])


def validate_mes_sales_order_response(response_payload):
	if not isinstance(response_payload, dict):
		frappe.throw(_("MES 销售订单推送接口响应格式异常。"))

	if not response_payload.get("success"):
		message = response_payload.get("message") or _("MES 销售订单推送失败。")
		error_code = response_payload.get("errorCode")
		if error_code:
			message = _("{0}，错误码：{1}").format(message, error_code)
		frappe.throw(message)


def format_date_value(value):
	if not value:
		return None
	return frappe.utils.getdate(value).isoformat()


def compact_dict(data):
	return {key: value for key, value in data.items() if value not in (None, "", [])}


@frappe.whitelist(methods=["POST"])
def reject_sales_order(sales_order_name, remark=None):
	"""Reject and cancel a submitted Sales Order while it is waiting for deposit confirmation."""
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	if not is_crm_integration_enabled(sales_order.get("company")):
		throw_crm_integration_disabled(sales_order.get("company"))
	sales_order.check_permission("cancel")
	assert_sales_order_not_closed(sales_order)

	if sales_order.docstatus != 1:
		frappe.throw(_("只有已提交的销售订单可以驳回并取消。"))

	if sales_order.get("custom_process_status") != PENDING_DEPOSIT_CONFIRMATION:
		frappe.throw(_("只有待确认定金的销售订单可以驳回。"))

	push_sales_order_status_to_crm(sales_order, CRM_STATUS_FINANCE_REJECTED, remark=remark)
	sales_order.cancel()
	sales_order.db_set("custom_process_status", REJECTED, update_modified=True)
	sales_order.custom_process_status = REJECTED
	sales_order.notify_update()
	frappe.logger().info(f"Sales Order {sales_order_name} rejected and cancelled from production flow")

	return {
		"status": "success",
		"message": _("销售订单已驳回并取消。"),
		"process_status": REJECTED,
		"docstatus": sales_order.docstatus,
		"timestamp": now(),
	}


def set_pending_deposit_confirmation_on_submit(doc, method=None):
	"""Move the production flow forward after the native ERPNext submit succeeds."""
	if not is_crm_integration_enabled(doc.get("company")):
		return

	if doc.get("custom_process_status") == REJECTED:
		return

	doc.db_set("custom_process_status", PENDING_DEPOSIT_CONFIRMATION, update_modified=True)
	doc.custom_process_status = PENDING_DEPOSIT_CONFIRMATION
	doc.notify_update()


def set_cancelled_process_status_on_cancel(doc, method=None):
	"""Keep the custom production flow status aligned with native cancellation."""
	if not is_crm_integration_enabled(doc.get("company")):
		return

	doc.db_set("custom_process_status", CANCELLED, update_modified=True)
	doc.custom_process_status = CANCELLED
	doc.notify_update()


def prevent_rejected_sales_order_submit(doc, method=None):
	"""Rejected CRM/MES flow orders must not be submitted later."""
	if not is_crm_integration_enabled(doc.get("company")):
		return

	if doc.get("custom_process_status") == REJECTED:
		frappe.throw(_("已驳回的销售订单不能提交。"))


def prevent_deposit_confirmation_processing_cancel(doc, method=None):
	"""Do not cancel an order while its external confirmation is in flight."""
	if not is_crm_integration_enabled(doc.get("company")):
		return

	if doc.get("custom_process_status") == DEPOSIT_CONFIRMATION_PROCESSING:
		frappe.throw(_("定金确认正在后台同步 CRM 和 MES，暂时不能取消销售订单。"))


@frappe.whitelist(methods=["POST"])
def confirm_deposit_and_push_to_mes(sales_order_name):
	"""Validate the action and queue external synchronization after commit."""
	from crm_integration.crm_integration.finance_release import AUDIT_EVENT, has_release_permission, read_order, review_order

	# Recheck under the row lock so simultaneous confirmations share the existing job.
	frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
	sales_order = read_order(sales_order_name)
	if not is_crm_integration_enabled(sales_order.get("company")):
		throw_crm_integration_disabled(sales_order.get("company"))
	if not has_release_permission():
		frappe.throw(_("未获得生产放行权限；管理员可在角色权限管理中配置。"), frappe.PermissionError)
	assert_sales_order_not_closed(sales_order)

	if sales_order.docstatus != 1:
		frappe.throw(_("销售订单必须提交后才能确认定金。"))

	process_status = sales_order.get("custom_process_status")
	if process_status == DEPOSIT_CONFIRMATION_PROCESSING:
		return {
			"status": "success",
			"message": _("定金确认任务正在后台处理，请勿重复提交。"),
			"process_status": DEPOSIT_CONFIRMATION_PROCESSING,
			"queued": True,
			"idempotent_replay": True,
			"timestamp": now(),
		}

	if process_status != PENDING_DEPOSIT_CONFIRMATION:
		frappe.throw(_("只有待确认定金的销售订单可以推送至MES。"))

	review = review_order(sales_order)
	if not review["can_release"]:
		frappe.throw(review["reason"])
	validate_mes_sync_available(sales_order)
	audit = create_crm_log(direction="Outbound", event=AUDIT_EVENT, status="Pending",
		reference_doctype="Sales Order", reference_name=sales_order.name, source="Desk",
		request_payload={"manual_confirmation": True, "receipts": review["receipts"], "receipts_complete": review["receipts_complete"]})
	if not audit:
		frappe.throw(_("无法保存逐单确认记录，未执行放行。"))
	set_process_status(sales_order, DEPOSIT_CONFIRMATION_PROCESSING)
	enqueue_confirm_deposit_and_push_to_mes(sales_order.name, audit.name)

	return {
		"status": "success",
		"message": _("定金确认任务已提交，系统将在后台同步 CRM 和 MES。"),
		"process_status": DEPOSIT_CONFIRMATION_PROCESSING,
		"queued": True,
		"timestamp": now(),
	}


def enqueue_confirm_deposit_and_push_to_mes(sales_order_name, audit_log=None):
	frappe.enqueue(
		"crm_integration.crm_integration.sales_order.confirm_deposit_and_push_to_mes_job",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"crm-confirm-deposit-{sales_order_name}",
		deduplicate=True,
		sales_order_name=sales_order_name,
		audit_log=audit_log,
	)


def confirm_deposit_and_push_to_mes_job(sales_order_name, audit_log=None):
	"""Synchronize the confirmed order; retries are safe through stable external identities."""
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	if not is_crm_integration_enabled(sales_order.get("company")):
		return None

	try:
		with crm_sales_order_creation_lock(sales_order.get("custom_crm_order_no")):
			frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
			sales_order = frappe.get_doc("Sales Order", sales_order_name)
			if sales_order.get("custom_process_status") != DEPOSIT_CONFIRMATION_PROCESSING:
				return None
			result = run_confirm_deposit_sync(sales_order)
		if audit_log:
			from crm_integration.crm_integration.finance_release import update_finance_audit
			update_finance_audit(audit_log, sales_order_name, "Success", result["process_status"])
		return result
	except Exception:
		error_message = frappe.get_traceback()
		frappe.db.rollback()
		restore_deposit_confirmation_after_failure(sales_order_name)
		if audit_log:
			from crm_integration.crm_integration.finance_release import update_finance_audit
			update_finance_audit(audit_log, sales_order_name, "Failed", PENDING_DEPOSIT_CONFIRMATION)
		create_crm_log(
			direction="Outbound",
			event="Confirm Deposit External Sync",
			status="Failed",
			reference_doctype="Sales Order",
			reference_name=sales_order_name,
			source="ERPNext",
			request_payload={"sales_order": sales_order_name},
			error_message=error_message,
		)
		frappe.db.commit()
		raise


def restore_deposit_confirmation_after_failure(sales_order_name):
	"""Allow an operator to retry after the queued external sync fails."""
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	if sales_order.get("custom_process_status") != DEPOSIT_CONFIRMATION_PROCESSING:
		return

	sales_order.db_set(
		"custom_process_status",
		PENDING_DEPOSIT_CONFIRMATION,
		update_modified=True,
	)
	sales_order.custom_process_status = PENDING_DEPOSIT_CONFIRMATION
	sales_order.notify_update()


def run_confirm_deposit_sync(sales_order):
	assert_sales_order_not_closed(sales_order)
	if sales_order.docstatus != 1:
		frappe.throw(_("销售订单必须提交后才能确认定金。"))

	process_status = sales_order.get("custom_process_status")
	if process_status == PENDING_PRODUCTION:
		return {
			"status": "success",
			"message": _("销售订单已经完成定金确认同步。"),
			"process_status": PENDING_PRODUCTION,
			"idempotent_replay": True,
		}
	if process_status not in {
		PENDING_DEPOSIT_CONFIRMATION,
		DEPOSIT_CONFIRMATION_PROCESSING,
	}:
		frappe.throw(_("只有待确认定金的销售订单可以推送至MES。"))

	push_sales_order_status_to_crm(sales_order, CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION)
	if not cint(sales_order.get("custom_crm_production_started")):
		# An E3 timeout may mean CRM accepted the event; persist the attempt first.
		sales_order.db_set("custom_crm_production_milestone", "ATTEMPTED", update_modified=True)
		sales_order.custom_crm_production_milestone = "ATTEMPTED"
		frappe.db.commit()
	push_sales_order_status_to_crm(sales_order, CRM_STATUS_IN_PRODUCTION)
	if not cint(sales_order.get("custom_crm_production_started")):
		sales_order.db_set({"custom_crm_production_started": 1,
			"custom_crm_production_milestone": "REACHED"}, update_modified=True)
		sales_order.custom_crm_production_started = 1
		sales_order.custom_crm_production_milestone = "REACHED"
		frappe.db.commit()
	push_sales_order_to_mes(sales_order)
	set_process_status(sales_order, PENDING_PRODUCTION)

	return {
		"status": "success",
		"message": _("定金已确认，销售订单已推送至MES。"),
		"process_status": PENDING_PRODUCTION,
		"timestamp": now(),
	}


@frappe.whitelist(methods=["POST"])
def reconcile_final_payment(sales_order_name):
	"""Release Sales Order for delivery after user confirmation."""
	sales_order = frappe.get_doc("Sales Order", sales_order_name)
	if not is_crm_integration_enabled(sales_order.get("company")):
		throw_crm_integration_disabled(sales_order.get("company"))
	sales_order.check_permission("write")
	assert_sales_order_not_closed(sales_order)

	if sales_order.docstatus != 1:
		frappe.throw(_("销售订单必须提交后才能核销尾款。"))

	from crm_integration.crm_integration.finance_release import assert_production_released
	assert_production_released(sales_order.name, sales_order.get("company"))

	grand_total = flt(sales_order.get("grand_total"), sales_order.precision("grand_total"))
	advance_paid = flt(sales_order.get("advance_paid"), sales_order.precision("advance_paid"))

	set_process_status(sales_order, DELIVERABLE)
	frappe.logger().info(f"Sales Order {sales_order_name} final payment reconciled")
	enqueue_sales_order_status_to_crm(
		sales_order_name=sales_order.name,
		external_status=CRM_STATUS_PENDING_SHIPMENT,
		triggered_status=DELIVERABLE,
		trigger_event="final_payment_reconciled",
		remark=_("尾款已核销，销售订单已放行发货。"),
	)
	enqueue_mes_sales_order_status_callback(
		sales_order.name,
		triggered_status=DELIVERABLE,
		trigger_event="final_payment_reconciled",
	)

	return {
		"status": "success",
		"message": _("尾款已核销，销售订单已放行发货。"),
		"process_status": DELIVERABLE,
		"advance_paid": advance_paid,
		"grand_total": grand_total,
		"timestamp": now(),
	}


def enqueue_mes_sales_order_status_callback(
	sales_order_name, triggered_status=None, trigger_event=None
):
	try:
		from mes_integration.mes_integration.sales_order import (
			enqueue_sales_order_status_callback,
		)

		enqueue_sales_order_status_callback(
			sales_order_name,
			triggered_status=triggered_status,
			trigger_event=trigger_event,
		)
	except ModuleNotFoundError as exc:
		if is_missing_mes_integration_error(exc):
			return
		raise
	except Exception:
		frappe.log_error(
			title="Failed to enqueue MES Sales Order status callback",
			message=frappe.get_traceback(),
		)
