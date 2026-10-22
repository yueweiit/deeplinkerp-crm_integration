import hashlib
import json
import math
from contextlib import ExitStack, contextmanager
from datetime import timedelta

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_time, getdate, now, nowdate, nowtime

from crm_integration.crm_integration.integration_log import create_crm_log, update_crm_log
from crm_integration.crm_integration.sales_order import (
	CRM_STATUS_SHIPMENT_CANCELLED,
	CRM_STATUS_SHIPMENT_CREATED,
	DELIVERABLE,
	PENDING_FINAL_PAYMENT,
	PENDING_PRODUCTION,
	enqueue_sales_order_status_to_crm,
	is_missing_mes_integration_error,
	set_process_status,
)
from crm_integration.crm_integration.sales_order_identity import get_sales_order_names_by_crm_order_no
from crm_integration.crm_integration.settings import is_crm_integration_enabled
from crm_integration.patches.add_crm_custom_fields import CRM_TEMPORARY_RECEIPT_TYPE

COMPLETED = "Completed"
PARTIALLY_DELIVERED = "Partially Delivered"
DRAFT_ALLOWED_STATUSES = (PENDING_PRODUCTION, PENDING_FINAL_PAYMENT, DELIVERABLE, PARTIALLY_DELIVERED)
CRM_SHIPMENT_FIELD = "custom_crm_shipment_no"
CRM_SHIPMENT_LOCK_TIMEOUT = 15
CRM_ROW_FIELD = "custom_crm_order_item_id"
TEMPORARY_VALUATION_POLICY = "CRM_ORDER_EFFECTIVE_PRICE_TEMPORARY"
SALES_ORDER_ITEM_PRICING_FIELDS = (
	"price_list_rate",
	"base_price_list_rate",
	"margin_type",
	"margin_rate_or_amount",
	"rate_with_margin",
	"base_rate_with_margin",
	"discount_percentage",
	"discount_amount",
	"pricing_rules",
	"is_free_item",
	"rate",
	"base_rate",
	"item_tax_template",
	"item_tax_rate",
)


class CRMShipmentIdentityConflict(frappe.ValidationError):
	http_status_code = 409


class CRMShipmentSalesOrderNotFound(frappe.ValidationError):
	http_status_code = 404


def create_and_submit_delivery_note_from_crm(payload):
	payload = normalize_crm_shipment_payload(payload)
	validate_crm_shipment_payload(payload)
	receipt = get_crm_frozen_committed_receipt(payload)
	if receipt:
		return receipt
	sales_order = get_crm_shipment_sales_order(payload)

	if not is_crm_integration_enabled(sales_order.get("company")):
		frappe.throw(_("公司 {0} 未启用 CRM 集成。").format(sales_order.get("company") or ""))

	crm_log = create_crm_log(
		direction="Inbound",
		event="CRM Shipment Create And Submit",
		status="Pending",
		reference_doctype="Sales Order",
		reference_name=sales_order.name,
		source="CRM",
		request_payload=payload,
	)

	try:
		with crm_shipment_lock(payload[CRM_SHIPMENT_FIELD]):
			lock_sales_order_for_crm_shipment(sales_order.name)
			sales_order = frappe.get_doc("Sales Order", sales_order.name, for_update=True)
			credit_delivery_note = get_crm_delivery_note_for_allocation_credit(payload)
			validate_crm_persisted_source(credit_delivery_note, payload)
			if credit_delivery_note and credit_delivery_note.docstatus == 2:
				throw_crm_shipment_identity_conflict(_("本 CRM 事件对应的销售出库已取消。"))
			if not credit_delivery_note or credit_delivery_note.docstatus != 1:
				validate_crm_strict_execution_payload(payload)
			allocations = allocate_crm_shipment_items(
				sales_order,
				payload["items"],
				credit_delivery_note=credit_delivery_note,
				strict=is_strict_crm_shipment(payload),
			)
			if credit_delivery_note and credit_delivery_note.docstatus == 1:
				validate_crm_delivery_note_identity(credit_delivery_note, sales_order, allocations)
				delivery_note, idempotent_replay = credit_delivery_note, True
			else:
				lock_rows, blockers = [], []
				for allocation in allocations:
					warehouse = resolve_crm_shipment_warehouse(sales_order, allocation, blockers, strict=is_strict_crm_shipment(payload))
					lock_rows.append(dict(item_code=allocation.sales_order_item.item_code, warehouse=warehouse))
				if blockers:
					frappe.throw("\n".join(blockers))
				with crm_stock_pool_locks(lock_rows):
					plan = build_crm_stock_plan(sales_order, payload, allocations, for_update=True)
					preview = get_crm_preview_response(sales_order, payload, plan)
					validate_crm_preview_fingerprints(payload, preview)
					if plan["blockers"]:
						frappe.throw("\n".join(plan["blockers"]))
					try:
						repost_started_at = now()
						release_sales_order_for_crm_shipment(sales_order, legacy_release=not is_strict_crm_shipment(payload))
						persist_crm_order_item_mappings(allocations)
						delivery_note, _replay = get_or_create_crm_delivery_note(sales_order, payload, allocations)
						supplement = make_crm_stock_supplement(sales_order, payload, plan, delivery_note.name) if payload.get("allow_stock_supplement") else None
						if supplement:
							delivery_note.set("custom_crm_stock_supplement", supplement.name)
							delivery_note.set("custom_crm_valuation_policy", TEMPORARY_VALUATION_POLICY)
						prepare_crm_delivery_note_for_submit(delivery_note, sales_order, payload)
						delivery_note.flags.ignore_permissions = True
						delivery_note.submit()
						record_crm_repost_dependencies(delivery_note, repost_started_at)
					except Exception:
						# Native controller boundaries and database deadlocks may
						# already have rolled back the whole transaction. This RPC
						# cannot continue after failure; a full rollback is safe in
						# either case and clears pending writes and callbacks.
						if not rollback_crm_shipment():
							# A failed cleanup detached the unusable database. Do not
							# reconnect just to update this transaction's audit row.
							crm_log = None
						raise
					idempotent_replay = False

		response = get_crm_shipment_response(
			delivery_note,
			payload[CRM_SHIPMENT_FIELD],
			idempotent_replay=idempotent_replay,
			for_update=True,
		)
		update_crm_log(
			crm_log,
			status="Success",
			reference_doctype="Delivery Note",
			reference_name=delivery_note.name,
			response_payload=response,
			http_status_code=200,
		)
		return response
	except Exception:
		update_crm_log(
			crm_log,
			status="Failed",
			error_message=frappe.get_traceback(),
			http_status_code=getattr(frappe.local.response, "http_status_code", None),
		)
		raise


def rollback_crm_shipment():
	"""Abort the request without letting cleanup replace its original error."""
	try:
		frappe.db.rollback()
	except Exception as rollback_error:
		db = getattr(frappe.local, "db", None)
		try:
			if db is not None:
				db.close()
		except Exception as close_error:
			log_crm_shipment_cleanup_failure("close_failed", close_error)
		finally:
			# Closing alone permits native SQL to reconnect. The HTTP exception
			# handler also rolls back again, so detach this failed request's DB.
			frappe.local.db = None
		log_crm_shipment_cleanup_failure("rollback_failed", rollback_error)
		return False
	return True


def log_crm_shipment_cleanup_failure(event, error):
	try:
		# No payload, credentials, SQL or native exception text in diagnostics.
		frappe.logger("crm_integration", allow_site=True).error(
			"CRM shipment transaction cleanup failed: %s (%s)", event, type(error).__name__
		)
	except Exception:
		pass  # Diagnostics must not replace the business error or prevent close.


def normalize_crm_shipment_payload(payload):
	if not isinstance(payload, dict):
		return payload

	payload = dict(payload)
	payload[CRM_SHIPMENT_FIELD] = (
		payload.get(CRM_SHIPMENT_FIELD) or payload.get("shipment_no") or payload.get("externalShipmentId")
	)
	for field in ("source_organization", "event_id"):
		if payload.get(field) is not None:
			payload[field] = str(payload[field]).strip()
	if payload.get("source_organization") and payload.get("event_id"):
		payload[CRM_SHIPMENT_FIELD] = "crm-event-" + crm_shipment_fingerprint(
			[payload["source_organization"], payload["event_id"]]
		)
	return payload


def validate_crm_persisted_source(delivery_note, payload):
	actual = get_crm_shipment_source_fingerprint(payload)
	if payload.get("source_fingerprint") and payload["source_fingerprint"] != actual:
		throw_crm_shipment_identity_conflict(_("CRM 来源内容与 source_fingerprint 不一致，请重新预览。"))
	if delivery_note and delivery_note.get("custom_crm_source_fingerprint"):
		if delivery_note.custom_crm_source_fingerprint != actual:
			throw_crm_shipment_identity_conflict(_("同一 CRM 事件的来源内容已变化，不能复用原出库。"))


def validate_crm_strict_execution_payload(payload):
	if not is_strict_crm_shipment(payload):
		return
	if not payload.get("source_fingerprint") or not payload.get("preview_fingerprint"):
		frappe.throw(_("新事件执行必须提供 source_fingerprint 和 preview_fingerprint，请先预览。"))
	blockers = get_crm_source_row_contract_blockers(payload)
	if blockers:
		frappe.throw("\n".join(blockers))


def get_crm_source_row_contract_blockers(payload):
	blockers = []
	for field in ("source_organization", "event_id"):
		if not payload.get(field):
			blockers.append(_("新事件执行必须提供 {0}，人工批号不能作为事件唯一身份。").format(field))
	for index, row in enumerate(payload["items"], 1):
		for field in ("crm_order_item_id", "item_code", "custom_version", "uom"):
			if field not in row or row[field] is None or (field != "custom_version" and not row[field]):
				blockers.append(_("第 {0} 行执行必须提供 {1}；custom_version 可显式为空。").format(index, field))
	return blockers


def validate_crm_shipment_payload(payload):
	if not isinstance(payload, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))

	shipment_no = str(payload.get(CRM_SHIPMENT_FIELD) or "").strip()
	if not shipment_no:
		frappe.throw(_("缺少必填字段：custom_crm_shipment_no"))
	if len(shipment_no) > 140:
		frappe.throw(_("custom_crm_shipment_no 不能超过 140 个字符。"))
	payload[CRM_SHIPMENT_FIELD] = shipment_no
	if bool(payload.get("source_organization")) != bool(payload.get("event_id")):
		frappe.throw(_("source_organization 和 event_id 必须同时提供。"))
	for field in ("source_organization", "event_id", "batch_no"):
		if payload.get(field) is not None and len(str(payload[field])) > 140:
			frappe.throw(_("{0} 不能超过 140 个字符。").format(field))
	for field in ("historical_replay", "allow_stock_supplement"):
		if field in payload and payload[field] not in (True, False, 0, 1):
			frappe.throw(_("{0} 必须是布尔值。").format(field))
	if is_strict_crm_shipment(payload):
		if not payload.get("posting_date") or not payload.get("posting_time"):
			frappe.throw(_("必须提供真实业务 posting_date 和 posting_time。"))
	if payload.get("posting_date"):
		payload["posting_date"] = str(getdate(payload["posting_date"]))
	if payload.get("posting_time"):
		payload["posting_time"] = str(get_time(payload["posting_time"]))

	if not payload.get("sales_order") and not payload.get("custom_crm_order_no"):
		frappe.throw(_("sales_order 和 custom_crm_order_no 至少需要提供一个。"))
	if "delivery_note" in payload:
		frappe.throw(
			_("delivery_note 参数已停用，请由 CRM 提供发货明细，ERP 将直接创建并提交销售出库。")
		)

	items = payload.get("items")
	if not isinstance(items, list) or not items:
		frappe.throw(_("缺少销售出库明细：items"))

	for index, item in enumerate(items, start=1):
		if not isinstance(item, dict):
			frappe.throw(_("items 中第 {0} 项必须是对象。").format(index))
		if not item.get("item_code") and not (item.get("sales_order_item") or item.get("name")):
			frappe.throw(_("销售出库第 {0} 行缺少 item_code 或 sales_order_item。").format(index))
		if not math.isfinite(flt(item.get("qty"))) or flt(item.get("qty")) <= 0:
			frappe.throw(_("销售出库第 {0} 行 qty 必须大于 0。").format(index))
		version = item.get("custom_version")
		if version is not None and not isinstance(version, str):
			frappe.throw(_("销售出库第 {0} 行 custom_version 必须是字符串。").format(index))
		if version is not None and len(version) > 64:
			frappe.throw(_("销售出库第 {0} 行 custom_version 不能超过 64 个字符。").format(index))
		if payload.get("event_id") and not (item.get("crm_order_item_id") or item.get("sales_order_item")):
			frappe.throw(_("销售出库第 {0} 行必须提供 crm_order_item_id 或 sales_order_item。").format(index))


def get_crm_shipment_sales_order(payload):
	sales_order_name = payload.get("sales_order")
	crm_order_no = payload.get("custom_crm_order_no")

	if sales_order_name:
		if not frappe.db.exists("Sales Order", sales_order_name):
			frappe.throw(_("未找到销售订单 {0}。").format(sales_order_name), exc=CRMShipmentSalesOrderNotFound)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)
		if crm_order_no and sales_order.get("custom_crm_order_no") != crm_order_no:
			throw_crm_shipment_identity_conflict(
				_("ERP 销售订单 {0} 与 CRM 订单号 {1} 不匹配。").format(sales_order_name, crm_order_no)
			)
		return sales_order

	names = get_sales_order_names_by_crm_order_no(crm_order_no, docstatus=1)
	if not names:
		frappe.throw(_("未找到 CRM 销售订单号 {0} 对应的销售订单。").format(crm_order_no), exc=CRMShipmentSalesOrderNotFound)
	if len(names) > 1:
		throw_crm_shipment_identity_conflict(_("CRM 销售订单号 {0} 对应多笔有效销售订单。").format(crm_order_no))
	return frappe.get_doc("Sales Order", names[0])


def get_crm_delivery_note_for_allocation_credit(payload):
	return get_delivery_note_by_crm_shipment_no(payload[CRM_SHIPMENT_FIELD], for_update=True)


def allocate_crm_shipment_items(sales_order, requested_items, credit_delivery_note=None, *, strict=False):
	if sales_order.docstatus != 1:
		frappe.throw(_("销售订单 {0} 必须已提交。").format(sales_order.name))

	credited_qty = get_delivery_note_qty_credit(credit_delivery_note, sales_order.name)
	available_rows = []
	for row in sales_order.get("items", []):
		if row.get("delivered_by_supplier"):
			continue
		remaining_qty = flt(row.get("qty")) - flt(row.get("delivered_qty")) + flt(credited_qty.get(row.name))
		if remaining_qty > 0:
			available_rows.append(frappe._dict(doc=row, remaining_qty=remaining_qty))

	allocations = []
	for index, request_item in enumerate(requested_items, start=1):
		request_row = frappe._dict(request_item)
		item_code = request_row.get("item_code")
		row_name = request_row.get("sales_order_item") or request_row.get("name")
		crm_row_id = request_row.get("crm_order_item_id")
		version_supplied = "custom_version" in request_item
		version = request_row.get("custom_version") or ""

		candidates = [row for row in available_rows if row.remaining_qty > 0]
		if row_name:
			candidates = [row for row in candidates if row.doc.name == row_name]
		elif crm_row_id:
			candidates = [row for row in candidates if str(row.doc.get(CRM_ROW_FIELD) or "") == str(crm_row_id)]
			if not candidates:
				candidates = [row for row in available_rows if row.remaining_qty > 0
					and not row.doc.get(CRM_ROW_FIELD) and row.doc.item_code == item_code
					and (not version_supplied or (row.doc.get("custom_version") or "") == version)
					and (not request_row.get("uom") or row.doc.get("uom") == request_row.uom)]
		else:
			candidates = [row for row in candidates if row.doc.item_code == item_code]
			if version_supplied:
				candidates = [row for row in candidates if (row.doc.get("custom_version") or "") == version]
			else:
				candidate_versions = {row.doc.get("custom_version") or "" for row in candidates}
				if len(candidate_versions) > 1:
					frappe.throw(
						_(
							"销售出库第 {0} 行物料 {1} 对应多个版本，请传 custom_version 或 sales_order_item。"
						).format(index, item_code)
					)

		if crm_row_id:
			if any(row.doc.get(CRM_ROW_FIELD) and str(row.doc.get(CRM_ROW_FIELD)) != str(crm_row_id) for row in candidates):
				throw_crm_shipment_identity_conflict(_("第 {0} 行 crm_order_item_id 与销售订单明细不匹配。").format(index))
			if len(candidates) != 1:
				throw_crm_shipment_identity_conflict(_("第 {0} 行 crm_order_item_id 未唯一对应销售订单明细。").format(index))
		if strict and not row_name and len(candidates) > 1:
			frappe.throw(_("第 {0} 行对应多条订单明细，请传 sales_order_item 或 crm_order_item_id。").format(index))
		if not candidates:
			frappe.throw(_("销售出库第 {0} 行未找到可出库的销售订单明细。").format(index))

		if row_name or crm_row_id:
			source_row = candidates[0].doc
			if item_code and source_row.item_code != item_code:
				throw_crm_shipment_identity_conflict(
					_("销售出库第 {0} 行 item_code 与 sales_order_item 不匹配。").format(index)
				)
			if version_supplied and (source_row.get("custom_version") or "") != version:
				throw_crm_shipment_identity_conflict(
					_("销售出库第 {0} 行 custom_version 与销售订单明细不匹配。").format(index)
				)
		if (strict or crm_row_id) and request_row.get("uom") and any(row.doc.get("uom") != request_row.uom for row in candidates):
			throw_crm_shipment_identity_conflict(_("第 {0} 行 uom 与销售订单明细不匹配。").format(index))

		qty_to_allocate = flt(request_row.get("qty"))
		for candidate in candidates:
			if qty_to_allocate <= 0:
				break
			allocated_qty = min(qty_to_allocate, flt(candidate.remaining_qty))
			allocations.append(
				frappe._dict(
					sales_order_item=candidate.doc,
					request_row=request_row,
					qty=allocated_qty,
					crm_order_item_id=str(crm_row_id) if crm_row_id else candidate.doc.get(CRM_ROW_FIELD),
				)
			)
			candidate.remaining_qty = flt(candidate.remaining_qty) - allocated_qty
			qty_to_allocate -= allocated_qty

		if qty_to_allocate > 0.000001:
			frappe.throw(_("销售出库第 {0} 行数量超过销售订单剩余可出库数量。").format(index))

	mappings, reverse_mappings = {}, {}
	for allocation in allocations:
		identity, row_name = allocation.get("crm_order_item_id"), allocation.sales_order_item.name
		if not identity:
			continue
		if row_name in mappings and mappings[row_name] != identity:
			throw_crm_shipment_identity_conflict(_("一条 ERP 明细不能绑定多个 crm_order_item_id。"))
		if identity in reverse_mappings and reverse_mappings[identity] != row_name:
			throw_crm_shipment_identity_conflict(_("crm_order_item_id 不能绑定多条 ERP 明细。"))
		if any(str(row.get(CRM_ROW_FIELD) or "") == identity and row.name != row_name for row in sales_order.get("items", [])):
			throw_crm_shipment_identity_conflict(_("crm_order_item_id 已绑定其他 ERP 明细。"))
		mappings[row_name], reverse_mappings[identity] = identity, row_name
	return allocations


def persist_crm_order_item_mappings(allocations):
	mappings = {}
	for allocation in allocations:
		identity = allocation.get("crm_order_item_id")
		row = allocation.sales_order_item
		if not identity:
			continue
		if row.name in mappings and mappings[row.name] != identity:
			throw_crm_shipment_identity_conflict(_("一条 ERP 订单明细不能绑定多个 crm_order_item_id。"))
		mappings[row.name] = identity
		if row.get(CRM_ROW_FIELD) and str(row.get(CRM_ROW_FIELD)) != identity:
			throw_crm_shipment_identity_conflict(_("crm_order_item_id 已绑定其他来源明细。"))
	for allocation in allocations:
		row = allocation.sales_order_item
		if row.name in mappings and not row.get(CRM_ROW_FIELD):
			row.db_set(CRM_ROW_FIELD, mappings[row.name], update_modified=False)


def get_crm_historical_parent_creation_blockers(company=None):
	"""Historical order creation must not send existing business notifications."""
	blockers = []
	for doctype, filters in (("Draft Notification Rule", dict(enabled=1, document_type="Sales Order")),
		("Notification", dict(enabled=1, document_type="Sales Order"))):
		if not frappe.db.exists("DocType", doctype):
			continue
		for rule in frappe.get_all(doctype, filters=filters, fields=["name", "company"] if doctype == "Draft Notification Rule" else ["name"]):
			if not company or not rule.get("company") or rule.company == company:
				blockers.append(_("历史补建 ERP 销售订单会触发已启用通知 {0}；须先确认无通知的订单完善方式。").format(rule.name))
	return blockers


def get_crm_historical_order_completion_blockers():
	"""Submitted-order edits may invoke Frappe value/method notifications through on_change."""
	if not frappe.db.exists("DocType", "Notification"):
		return []
	return [_("历史补全 ERP 销售订单可能触发已启用通知 {0}，须先确认无通知的完善方式。").format(rule.name)
		for rule in frappe.get_all("Notification", filters=dict(enabled=1, document_type="Sales Order",
			event=["in", ["Value Change", "Method"]]), fields=["name"])]


def get_crm_frozen_committed_receipt(payload):
	"""Recover an already committed fact without depending on mutable parent readiness."""
	if not payload.get("source_fingerprint") or not payload.get("source_organization") or not payload.get("event_id"):
		return None
	delivery = get_delivery_note_by_crm_shipment_no(payload[CRM_SHIPMENT_FIELD])
	if not delivery or delivery.docstatus != 1:
		return None
	stored = delivery.get("custom_crm_source_fingerprint")
	# Legacy receipts without frozen evidence retain the original allocation checks.
	if not isinstance(stored, str) or len(stored) != 64:
		return None
	validate_crm_persisted_source(delivery, payload)
	response = get_crm_shipment_response(delivery, payload[CRM_SHIPMENT_FIELD], idempotent_replay=True)
	response.update(can_execute=True, company=delivery.company, currency=delivery.get("currency"),
		sales_order=payload.get("sales_order"), blockers=[], items=[dict(sales_order_item=row.get("so_detail"),
			item_code=row.item_code, uom=row.get("uom"), qty=row.qty, warehouse=row.get("warehouse"))
			for row in delivery.get("items", [])])
	return response


def preview_delivery_note_from_crm(payload):
	"""Inspect the same native plan used by execution without logging, locks or writes."""
	payload = normalize_crm_shipment_payload(payload)
	validate_crm_shipment_payload(payload)
	receipt = get_crm_frozen_committed_receipt(payload)
	if receipt:
		return receipt
	try:
		sales_order = get_crm_shipment_sales_order(payload)
	except frappe.ValidationError as exc:
		return dict(status="blocked", can_execute=False, sales_order=None, items=[], blockers=[str(exc)],
			blocker_code="ERP_CRM_SALES_ORDER_NOT_FOUND" if isinstance(exc, CRMShipmentSalesOrderNotFound) else "ERP_CRM_SHIPMENT_IDENTITY_CONFLICT",
			source_fingerprint=get_crm_shipment_source_fingerprint(payload),
			parent_creation_blockers=get_crm_historical_parent_creation_blockers(payload.get("parent_company"))
				if isinstance(exc, CRMShipmentSalesOrderNotFound) and payload.get("historical_replay") else [])
	try:
		if not is_crm_integration_enabled(sales_order.get("company")):
			frappe.throw(_("订单公司未启用 CRM 集成。"))
		existing = get_delivery_note_by_crm_shipment_no(payload[CRM_SHIPMENT_FIELD])
		validate_crm_persisted_source(existing, payload)
		completion = get_crm_order_completion_preview(sales_order, payload)
		if completion and (completion["blockers"] or completion["added_items"]):
			frappe.throw(_("完整 CRM manifest 与 ERP 订单存在待核对或待补明细。"))
		allocations = allocate_crm_shipment_items(sales_order, payload["items"], credit_delivery_note=existing, strict=is_strict_crm_shipment(payload))
		if existing and existing.docstatus == 1:
			validate_crm_delivery_note_identity(existing, sales_order, allocations)
			response = get_crm_shipment_response(existing, payload[CRM_SHIPMENT_FIELD], idempotent_replay=True)
			response.update(can_execute=True, order_fingerprint=get_crm_commercial_order_fingerprint(sales_order), sales_order=sales_order.name, sales_order_items=get_crm_native_order_rows(sales_order),
				company=sales_order.company, currency=sales_order.get("currency"), blockers=[], items=[dict(sales_order_item=a.sales_order_item.name,
					crm_order_item_id=a.get("crm_order_item_id"), item_code=a.sales_order_item.item_code,
					uom=a.sales_order_item.get("uom"), warehouse=a.sales_order_item.get("warehouse"), qty=a.qty)
					for a in allocations], source_fingerprint=get_crm_shipment_source_fingerprint(payload))
			return response
		plan = build_crm_stock_plan(sales_order, payload, allocations)
		if is_strict_crm_shipment(payload):
			plan["blockers"].extend(get_crm_source_row_contract_blockers(payload))
		if existing and existing.docstatus == 2:
			plan["blockers"].append(_("本 CRM 事件对应的销售出库已取消。"))
		return get_crm_preview_response(sales_order, payload, plan)
	except frappe.ValidationError as exc:
		response = dict(status="blocked", can_execute=False, sales_order=sales_order.name,
			company=sales_order.company, currency=sales_order.get("currency"), items=[],
			order_fingerprint=get_crm_commercial_order_fingerprint(sales_order),
			sales_order_items=get_crm_native_order_rows(sales_order), blockers=[str(exc)],
			blocker_code="ERP_CRM_ORDER_ITEM_MAPPING_REQUIRED", source_fingerprint=get_crm_shipment_source_fingerprint(payload))
		if isinstance(payload.get("_crm_order_items"), list):
			response["order_completion"] = get_crm_order_completion_preview(sales_order, payload)
			response["blockers"].extend(response["order_completion"]["blockers"])
		return response


def get_crm_order_completion_preview(sales_order, payload):
	if not isinstance(payload.get("_crm_order_items"), list):
		return None
	from crm_integration.crm_integration.api import (
		CRM_ORDER_SOURCE_ITEM_FIELDS,
		plan_missing_crm_sales_order_items,
	)
	manifest = [{key: value for key, value in row.items() if key in CRM_ORDER_SOURCE_ITEM_FIELDS}
		if isinstance(row, dict) else row for row in payload["_crm_order_items"]]
	for row in manifest:
		if isinstance(row, dict) and "version" in row and row["version"] is None:
			row["version"] = ""
	return plan_missing_crm_sales_order_items(sales_order, manifest,
		historical_replay=bool(payload.get("historical_replay")))


def get_crm_native_order_rows(sales_order):
	return [{field: row.get(field) for field in ("name", CRM_ROW_FIELD, "item_code", "custom_version", "uom",
		"stock_uom", "conversion_factor", "qty", "delivered_qty", "warehouse", "net_rate", "base_net_rate")}
		for row in sales_order.get("items", [])]


def get_crm_commercial_order_fingerprint(sales_order):
	"""Version commercial evidence while allowing native delivery progress between batches."""
	header_fields = ("name", "docstatus", "company", "customer", "currency", "conversion_rate", "transaction_date", "delivery_date",
		"selling_price_list", "price_list_currency", "plc_conversion_rate", "taxes_and_charges", "tax_category",
		"tax_withholding_category", "apply_discount_on", "additional_discount_percentage", "discount_amount",
		"base_discount_amount", "set_warehouse", "customer_address", "shipping_address_name", "company_address",
		"cost_center", "project", "incoterm", "named_place", "custom_crm_order_no",
		"custom_china_settlement_mode", "custom_china_settlement_rule", "custom_china_settlement_confirmation_method",
		"custom_china_settlement_override", "custom_china_settlement_override_reason")
	row_fields = ("name", "item_code", "custom_version", "qty", "uom", "stock_uom", "conversion_factor", "warehouse",
		"rate", "net_rate", "base_rate", "base_net_rate", "price_list_rate", "discount_percentage", "discount_amount",
		"delivery_date", "item_tax_template", "item_tax_rate", "income_account", "expense_account", "cost_center", "project")
	version = {field: sales_order.get(field) for field in header_fields}
	version["items"] = [{field: row.get(field) for field in row_fields} for row in sales_order.get("items", [])]
	# Tax row accounting and included-in-price flags affect native valuation as well as totals.
	version["taxes"] = [{key: value for key, value in (row.as_dict(no_private_properties=True) if callable(getattr(row, "as_dict", None)) else dict(row)).items()
		if key not in ("creation", "modified", "modified_by", "owner", "idx", "name", "parent", "parenttype", "parentfield")}
		for row in sales_order.get("taxes", [])]
	return crm_shipment_fingerprint(version)


def get_crm_preview_response(sales_order, payload, plan):
	source_fingerprint = get_crm_shipment_source_fingerprint(payload)
	order_snapshot = sales_order.as_dict(no_private_properties=True) if callable(getattr(sales_order, "as_dict", None)) else dict(sales_order)
	return dict(plan, status="blocked" if plan["blockers"] else "success", can_execute=not plan["blockers"],
		sales_order=sales_order.name, sales_order_items=get_crm_native_order_rows(sales_order),
		process_status=sales_order.get("custom_process_status"), source_fingerprint=source_fingerprint,
		order_fingerprint=get_crm_commercial_order_fingerprint(sales_order),
		blocker_code="ERP_CRM_NATIVE_BLOCKERS" if plan["blockers"] else None,
		preview_fingerprint=crm_shipment_fingerprint(dict(source_fingerprint=source_fingerprint,
			plan=plan, process_status=sales_order.get("custom_process_status"), status=sales_order.get("status"),
			order_snapshot=order_snapshot)))


def validate_crm_preview_fingerprints(payload, preview):
	for field in ("source_fingerprint", "preview_fingerprint"):
		if payload.get(field) and payload[field] != preview.get(field):
			throw_crm_shipment_identity_conflict(_("预览后来源、订单或库存已变化，请重新预览。"))


def get_delivery_note_qty_credit(delivery_note, sales_order_name):
	if not delivery_note or delivery_note.docstatus != 1:
		return {}

	quantities = {}
	for row in delivery_note.get("items", []):
		if row.get("against_sales_order") != sales_order_name or not row.get("so_detail"):
			continue
		quantities[row.so_detail] = flt(quantities.get(row.so_detail)) + flt(row.get("qty"))
	return quantities


def crm_shipment_fingerprint(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
		separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def get_crm_shipment_source_fingerprint(payload):
	# Transport aliases and preview tokens are not business source data.
	source = {key: payload.get(key) for key in (
		CRM_SHIPMENT_FIELD, "source_organization", "event_id", "batch_no", "sales_order",
		"custom_crm_order_no", "posting_date", "posting_time", "historical_replay",
		"allow_stock_supplement", "items")}
	# Preserve frozen receipts from payloads predating optional parent-creation evidence.
	if "parent_company" in payload:
		source["parent_company"] = payload["parent_company"]
	return crm_shipment_fingerprint(source)


def resolve_crm_shipment_warehouse(sales_order, allocation, blockers, *, strict=False):
	request = allocation.request_row
	warehouse = allocation.sales_order_item.get("warehouse") or sales_order.get("set_warehouse")
	if strict and request.get("warehouse") and warehouse and request.warehouse != warehouse:
		blockers.append(_("warehouse {0} 与销售订单行仓库 {1} 冲突。").format(request.warehouse, warehouse))
	warehouse = warehouse if strict else request.get("warehouse") or warehouse
	if not warehouse:
		blockers.append(_("销售订单明细 {0} 缺少 warehouse，须先维护原生仓库。").format(allocation.sales_order_item.name))
		return None
	values = frappe.db.get_value("Warehouse", warehouse, ["company", "disabled", "is_group"], as_dict=True)
	if not values or values.company != sales_order.company or cint(values.get("disabled")) or cint(values.get("is_group")):
		blockers.append(_("warehouse {0} 不存在、已停用、是组仓库或不属于订单公司。").format(warehouse))
	return warehouse


def build_crm_stock_plan(sales_order, payload, allocations, *, for_update=False):
	"""Read native historical stock once per pool; share it in CRM source row order."""
	from erpnext.stock.utils import get_stock_balance

	blockers, items, pools = [], [], {}
	base_currency = frappe.get_cached_value("Company", sales_order.company, "default_currency")
	posting_date = payload.get("posting_date") or nowdate()
	posting_time = payload.get("posting_time") or nowtime()
	if is_strict_crm_shipment(payload) and sales_order.get("custom_process_status") not in (DELIVERABLE, PARTIALLY_DELIVERED):
		blockers.append(_("当前待生产、待尾款或其他流程状态，尚未满足出库条件：{0}。").format(sales_order.get("custom_process_status") or ""))
	if sales_order.get("status") in ("Closed", "Cancelled", "Stopped", "On Hold"):
		blockers.append(_("销售订单已关闭、取消或暂停。"))
	for allocation in allocations:
		source, request = allocation.sales_order_item, allocation.request_row
		before = len(blockers)
		warehouse = resolve_crm_shipment_warehouse(sales_order, allocation, blockers, strict=is_strict_crm_shipment(payload))
		if is_strict_crm_shipment(payload):
			master = frappe.db.get_value("Item", source.item_code, ["disabled", "is_stock_item", "is_sales_item", "has_variants"], as_dict=True)
			if not master or cint(master.get("disabled")) or not cint(master.get("is_stock_item")) or not cint(master.get("is_sales_item")) or cint(master.get("has_variants")):
				blockers.append(_("{0} 必须是启用的库存销售物料，不能为模板、非库存或停用物料。").format(source.item_code))
		allocation.warehouse = warehouse
		factor = flt(source.get("conversion_factor")) or 1
		if is_strict_crm_shipment(payload) and (flt(source.get("conversion_factor")) <= 0 or not source.get("stock_uom") or not source.get("uom")):
			blockers.append(_("{0} 缺少有效的原生库存单位或 conversion_factor。").format(source.item_code))
		stock_qty = flt(allocation.qty) * factor
		key = (source.item_code, warehouse)
		if warehouse and len(blockers) == before and key not in pools:
			if for_update:
				from erpnext.stock.stock_ledger import get_previous_sle

				previous = get_previous_sle(frappe._dict(item_code=source.item_code, warehouse=warehouse,
					posting_date=posting_date, posting_time=posting_time), for_update=True)
				pools[key] = flt(previous.get("qty_after_transaction") if previous else 0)
			else:
				pools[key] = flt(get_stock_balance(source.item_code, warehouse, posting_date, posting_time))
		available = pools.get(key, 0)
		shortage = max(0, stock_qty - max(0, available))
		pools[key] = max(0, available - stock_qty)
		row = dict(sales_order_item=source.name, crm_order_item_id=request.get("crm_order_item_id") or source.get(CRM_ROW_FIELD),
			item_code=source.item_code, custom_version=source.get("custom_version") or "", qty=flt(allocation.qty),
			uom=source.get("uom"), stock_uom=source.get("stock_uom"), conversion_factor=factor,
			warehouse=warehouse, stock_qty=stock_qty, available_qty=available, shortage_qty=shortage,
			source_unit_price=request.get("source_unit_price"), source_currency=request.get("source_currency"),
			native_net_rate=source.get("net_rate"), temporary_valuation_rate=None, base_currency=base_currency)
		if is_strict_crm_shipment(payload) and available < -0.000001:
			blockers.append(_("{0} 在业务时点原生库存为负，须先核对库存账。").format(source.item_code))
		if shortage > 0.000001:
			if not payload.get("allow_stock_supplement"):
				if is_strict_crm_shipment(payload):
					blockers.append(_("{0} 库存不足 {1}，未授权成品临时补入库。").format(source.item_code, shortage))
			else:
				price = request.get("source_unit_price")
				price_precision = get_crm_order_rate_precision(source, "net_rate", sales_order.get("currency"))
				base_precision = get_crm_order_rate_precision(source, "base_net_rate", base_currency)
				if price is None or price == "":
					blockers.append(_("{0} 补入库缺少 source_unit_price；缺失不能按零处理。").format(source.item_code))
				elif not is_valid_crm_price(price) or source.get("net_rate") is None or flt(price, price_precision) != flt(source.net_rate, price_precision):
					blockers.append(_("{0} source_unit_price 与订单有效 net_rate 不一致。").format(source.item_code))
				elif request.get("source_currency") != sales_order.get("currency"):
					blockers.append(_("{0} source_currency 与订单币种不一致。").format(source.item_code))
				elif source.get("base_net_rate") is None or factor <= 0 or flt(sales_order.get("conversion_rate")) <= 0:
					blockers.append(_("{0} 缺少有效的原生本币价格或单位换算。").format(source.item_code))
				else:
					row["temporary_valuation_rate"] = flt(source.base_net_rate) / factor
					if flt(source.base_net_rate, base_precision) != flt(flt(source.net_rate) * flt(sales_order.conversion_rate), base_precision):
						blockers.append(_("{0} 原生订单本币价格与汇率不一致。").format(source.item_code))
		items.append(row)
	return dict(items=items, blockers=list(dict.fromkeys(blockers)), posting_date=posting_date,
		posting_time=posting_time, company=sales_order.company, currency=sales_order.get("currency"),
		base_currency=base_currency, valuation_policy=TEMPORARY_VALUATION_POLICY)


def is_valid_crm_price(value):
	try:
		return not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0
	except (TypeError, ValueError):
		return False


def get_crm_order_rate_precision(source, fieldname, currency):
	if callable(getattr(source, "precision", None)):
		return source.precision(fieldname)
	from frappe.model.meta import get_field_precision

	return get_field_precision(frappe.get_meta("Sales Order Item").get_field(fieldname), source, currency=currency)


def make_crm_stock_supplement(sales_order, payload, plan, delivery_note_name=None):
	shortages = [row for row in plan["items"] if row["shortage_qty"] > 0.000001]
	if not shortages:
		return None
	if not payload.get("allow_stock_supplement") or plan["blockers"]:
		frappe.throw(_("成品补入库未授权或预览存在阻断。"))
	# A separate native timestamp guarantees receipt SLE sorts before the DN SLE,
	# including historical midnight shipments. Native period/freeze/repost checks remain active.
	receipt_time = get_datetime(f"{plan['posting_date']} {plan['posting_time']}") - timedelta(seconds=1)
	items = []
	for row in shortages:
		rate = row["temporary_valuation_rate"]
		if rate is None:
			frappe.throw(_("缺少已核对的临时估值，不能补入库。"))
		items.append(dict(item_code=row["item_code"], qty=row["shortage_qty"], transfer_qty=row["shortage_qty"],
			uom=row["stock_uom"], stock_uom=row["stock_uom"], conversion_factor=1,
			t_warehouse=row["warehouse"], basic_rate=rate, set_basic_rate_manually=1,
			allow_zero_valuation_rate=int(rate == 0), custom_crm_order_item_id=row.get("crm_order_item_id"),
			custom_crm_sales_order_item=row["sales_order_item"]))
	entry = frappe.get_doc(dict(doctype="Stock Entry", stock_entry_type=CRM_TEMPORARY_RECEIPT_TYPE, purpose="Material Receipt",
		company=sales_order.company, posting_date=str(receipt_time.date()), posting_time=str(receipt_time.time()),
		set_posting_time=1, custom_crm_shipment_no=payload[CRM_SHIPMENT_FIELD],
		custom_crm_delivery_note=delivery_note_name, custom_crm_source_fingerprint=get_crm_shipment_source_fingerprint(payload),
		custom_crm_valuation_policy=TEMPORARY_VALUATION_POLICY,
		remarks=_("CRM 成品短缺临时补入库，按订单有效单价折算本币及库存单位临时估值；不代表实际生产成本。事件：{0}").format(payload[CRM_SHIPMENT_FIELD]),
		items=items))
	entry.flags.ignore_permissions = True
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry


def get_or_create_crm_delivery_note(sales_order, payload, allocations):
	shipment_no = payload[CRM_SHIPMENT_FIELD]
	existing = get_delivery_note_by_crm_shipment_no(shipment_no, for_update=True)

	if existing:
		validate_crm_delivery_note_identity(existing, sales_order, allocations)
		if existing.docstatus == 2:
			throw_crm_shipment_identity_conflict(
				_("CRM 发货单号 {0} 对应的 ERP 销售出库已取消。").format(shipment_no)
			)
		return existing, existing.docstatus == 1

	return make_crm_delivery_note(sales_order, payload, allocations), False


def get_delivery_note_by_crm_shipment_no(shipment_no, *, for_update=False):
	if not frappe.db.has_column("Delivery Note", CRM_SHIPMENT_FIELD):
		frappe.throw(_("缺少 CRM 发货单号字段，请先执行 bench migrate。"))

	if for_update:
		names = [row[0] for row in frappe.db.sql(
			"SELECT name FROM `tabDelivery Note` WHERE custom_crm_shipment_no = %s LIMIT 2 FOR UPDATE",
			(shipment_no,))]
	else:
		names = frappe.get_all("Delivery Note", filters={CRM_SHIPMENT_FIELD: shipment_no},
			pluck="name", limit_page_length=2)
	if len(names) > 1:
		throw_crm_shipment_identity_conflict(
			_("CRM 发货单号 {0} 已对应多张 ERP 销售出库。").format(shipment_no)
		)
	return frappe.get_doc("Delivery Note", names[0], for_update=for_update) if names else None


def validate_crm_delivery_note_identity(delivery_note, sales_order, allocations):
	error = get_crm_delivery_note_identity_error(delivery_note, sales_order, allocations)
	if error:
		throw_crm_shipment_identity_conflict(error)


def get_crm_delivery_note_identity_error(delivery_note, sales_order, allocations):
	linked_sales_orders = get_linked_sales_orders(delivery_note)
	if linked_sales_orders != [sales_order.name]:
		return _("ERP 销售出库 {0} 关联的销售订单与本次请求不一致。").format(
			delivery_note.name
		)

	expected_qty = get_allocation_qty_by_sales_order_item(allocations)
	actual_qty = {}
	for row in delivery_note.get("items", []):
		if row.get("against_sales_order") != sales_order.name or not row.get("so_detail"):
			continue
		actual_qty[row.so_detail] = flt(actual_qty.get(row.so_detail)) + flt(row.get("qty"))
	actual_qty = {key: flt(value, 9) for key, value in actual_qty.items()}
	if actual_qty != expected_qty:
		return _("ERP 销售出库 {0} 的明细与本次 CRM 发货数量不一致。").format(
			delivery_note.name
		)

	if not delivery_note_dimensions_match(delivery_note, allocations):
		return _("ERP 销售出库 {0} 的仓库、批次或序列号与本次 CRM 发货不一致。").format(
			delivery_note.name
		)

	return None


def delivery_note_dimensions_match(delivery_note, allocations):
	constraints = {}
	for allocation in allocations:
		warehouse = allocation.get("warehouse") or allocation.request_row.get("warehouse") or allocation.sales_order_item.get("warehouse") or None
		batch_no = allocation.request_row.get("batch_no") or None
		serial_no = allocation.request_row.get("serial_no") or None
		serial_and_batch_bundle = allocation.request_row.get("serial_and_batch_bundle") or None
		if not any((warehouse, batch_no, serial_no, serial_and_batch_bundle)):
			continue
		key = (
			allocation.sales_order_item.name,
			warehouse,
			batch_no,
			serial_no,
			serial_and_batch_bundle,
		)
		constraints[key] = flt(constraints.get(key)) + flt(allocation.qty)

	actual_by_so_detail = {}
	for row in delivery_note.get("items", []):
		if not row.get("so_detail"):
			continue
		actual_by_so_detail.setdefault(row.so_detail, []).append(
			frappe._dict(
				warehouse=row.get("warehouse") or None,
				batch_no=row.get("batch_no") or None,
				serial_no=row.get("serial_no") or None,
				serial_and_batch_bundle=row.get("serial_and_batch_bundle") or None,
				remaining_qty=flt(row.get("qty")),
			)
		)

	ordered_constraints = sorted(
		constraints.items(),
		key=lambda entry: sum(value is not None for value in entry[0][1:]),
		reverse=True,
	)
	for (
		so_detail,
		warehouse,
		batch_no,
		serial_no,
		serial_and_batch_bundle,
	), expected_qty in ordered_constraints:
		matching_rows = [
			row
			for row in actual_by_so_detail.get(so_detail, [])
			if row.remaining_qty > 0
			and (not warehouse or row.warehouse == warehouse)
			and (not batch_no or row.batch_no == batch_no)
			and (not serial_no or row.serial_no == serial_no)
			and (
				not serial_and_batch_bundle
				or row.serial_and_batch_bundle == serial_and_batch_bundle
			)
		]
		if flt(sum(row.remaining_qty for row in matching_rows), 9) + 0.000001 < flt(expected_qty, 9):
			return False

		qty_to_consume = flt(expected_qty)
		for row in matching_rows:
			consumed_qty = min(qty_to_consume, row.remaining_qty)
			row.remaining_qty -= consumed_qty
			qty_to_consume -= consumed_qty
			if qty_to_consume <= 0.000001:
				break

	return True


def get_allocation_qty_by_sales_order_item(allocations):
	quantities = {}
	for allocation in allocations:
		row_name = allocation.sales_order_item.name
		quantities[row_name] = flt(quantities.get(row_name)) + flt(allocation.qty)
	return {key: flt(value, 9) for key, value in quantities.items()}


def make_crm_delivery_note(sales_order, payload, allocations):
	delivery_note = make_delivery_note(
		sales_order.name,
		kwargs={"skip_item_mapping": True, "for_reserved_stock": False},
	)
	apply_locked_sales_order_header(delivery_note, sales_order)
	delivery_note.set("items", [])
	delivery_note.set(CRM_SHIPMENT_FIELD, payload[CRM_SHIPMENT_FIELD])
	delivery_note.ignore_pricing_rule = 1

	for allocation in allocations:
		append_crm_delivery_note_item(delivery_note, sales_order, allocation)

	apply_crm_shipment_header_fields(delivery_note, payload)
	delivery_note.run_method("set_missing_values")
	apply_sales_order_pricing_to_delivery_note(delivery_note, sales_order)
	delivery_note.run_method("set_po_nos")
	delivery_note.run_method("calculate_taxes_and_totals")
	delivery_note.run_method("set_use_serial_batch_fields")
	delivery_note.flags.ignore_permissions = True
	delivery_note.insert(ignore_permissions=True)
	return delivery_note


def apply_locked_sales_order_header(delivery_note, sales_order):
	"""Refresh native mapping from the locked source, including cleared values and tables."""
	from frappe.model import no_value_fields, table_fields
	from frappe.model.mapper import map_doc

	# Native mapper reloads its source through a repeatable-read snapshot. Refresh
	# same-name mapped header fields from the source already locked by execution.
	for field in sales_order.meta.fields:
		if field.no_copy or field.fieldtype in (*table_fields, *no_value_fields):
			continue
		if delivery_note.meta.has_field(field.fieldname):
			delivery_note.set(field.fieldname, sales_order.get(field.fieldname))
	map_doc(sales_order, delivery_note, {"doctype": "Delivery Note", "validation": {"docstatus": ["=", 1]}})
	for table in ("taxes", "sales_team"):
		delivery_note.set(table, [])
		for source in sales_order.get(table, []):
			target = delivery_note.append(table, {})
			map_doc(source, target, {"doctype": source.doctype}, sales_order)


def append_crm_delivery_note_item(delivery_note, sales_order, allocation):
	source = allocation.sales_order_item
	request_row = allocation.request_row
	qty = flt(allocation.qty)
	conversion_factor = flt(source.get("conversion_factor")) or 1
	warehouse = allocation.get("warehouse") or source.get("warehouse") or sales_order.get("set_warehouse") or request_row.get("warehouse")

	row = delivery_note.append(
		"items",
		{
			"item_code": source.item_code,
			"item_name": source.item_name,
			"description": source.description,
			"qty": qty,
			"stock_qty": qty * conversion_factor,
			"uom": source.uom,
			"stock_uom": source.stock_uom,
			"conversion_factor": conversion_factor,
			"warehouse": warehouse,
			"rate": source.rate,
			"base_rate": source.base_rate,
			"amount": qty * flt(source.rate),
			"base_amount": qty * flt(source.base_rate),
			"against_sales_order": sales_order.name,
			"so_detail": source.name,
			"delivery_date": source.get("delivery_date") or sales_order.get("delivery_date"),
			"custom_version": source.get("custom_version"),
			"custom_crm_order_item_id": allocation.get("crm_order_item_id") or source.get(CRM_ROW_FIELD),
			"custom_crm_source_unit_price": request_row.get("source_unit_price"),
			"custom_crm_source_currency": request_row.get("source_currency"),
			"custom_crm_source_uom": request_row.get("source_uom") or request_row.get("uom"),
		},
	)
	apply_sales_order_item_pricing(row, source)

	for fieldname in ("batch_no", "serial_no", "serial_and_batch_bundle"):
		if request_row.get(fieldname):
			row.set(fieldname, request_row.get(fieldname))


def prepare_crm_delivery_note_for_submit(delivery_note, sales_order, payload):
	set_crm_shipment_no(delivery_note, payload[CRM_SHIPMENT_FIELD])
	apply_crm_shipment_header_fields(delivery_note, payload)
	delivery_note.ignore_pricing_rule = 1
	versions = {row.name: row.get("custom_version") for row in sales_order.get("items", [])}
	for row in delivery_note.get("items", []):
		if row.get("so_detail") in versions:
			row.set("custom_version", versions[row.so_detail])

	apply_sales_order_pricing_to_delivery_note(delivery_note, sales_order)
	delivery_note.flags.ignore_permissions = True
	delivery_note.save(ignore_permissions=True)


def apply_sales_order_pricing_to_delivery_note(delivery_note, sales_order):
	"""Keep shipment pricing identical to the submitted Sales Order."""
	delivery_note.ignore_pricing_rule = 1
	items_by_name = {row.name: row for row in sales_order.get("items", [])}
	for row in delivery_note.get("items", []):
		source = items_by_name.get(row.get("so_detail"))
		if source:
			apply_sales_order_item_pricing(row, source)


def apply_sales_order_item_pricing(target, source):
	for fieldname in SALES_ORDER_ITEM_PRICING_FIELDS:
		target.set(fieldname, source.get(fieldname))

	qty = flt(target.get("qty"))
	target.amount = qty * flt(source.get("rate"))
	target.base_amount = qty * flt(source.get("base_rate"))


def apply_crm_shipment_header_fields(delivery_note, payload):
	if payload.get("posting_date"):
		delivery_note.posting_date = getdate(payload["posting_date"])
	if payload.get("posting_time"):
		delivery_note.posting_time = payload["posting_time"]
		delivery_note.set_posting_time = 1
	for source, target in (("source_organization", "custom_crm_source_organization"),
		("event_id", "custom_crm_event_id"), ("batch_no", "custom_crm_batch_no"),
		("historical_replay", "custom_crm_historical_replay")):
		if source in payload:
			delivery_note.set(target, payload[source])
	delivery_note.set("custom_crm_source_fingerprint", get_crm_shipment_source_fingerprint(payload))


def set_crm_shipment_no(delivery_note, shipment_no):
	current = delivery_note.get(CRM_SHIPMENT_FIELD)
	if current and current != shipment_no:
		throw_crm_shipment_identity_conflict(
			_("ERP 销售出库 {0} 已绑定其他 CRM 发货单号。").format(delivery_note.name)
		)
	if delivery_note.docstatus == 1:
		delivery_note.db_set(CRM_SHIPMENT_FIELD, shipment_no, update_modified=False)
	else:
		delivery_note.set(CRM_SHIPMENT_FIELD, shipment_no)


def is_strict_crm_shipment(payload):
	items = payload.get("items")
	items = items if isinstance(items, list) else []
	return any(field in payload for field in ("source_organization", "event_id", "batch_no", "historical_replay",
		"allow_stock_supplement", "source_fingerprint", "preview_fingerprint")) or any(
		any(field in row for field in ("crm_order_item_id", "source_unit_price", "source_currency", "source_uom"))
		for row in items if isinstance(row, dict))


def release_sales_order_for_crm_shipment(sales_order, legacy_release=False):
	process_status = sales_order.get("custom_process_status")
	if legacy_release and process_status in (PENDING_PRODUCTION, PENDING_FINAL_PAYMENT):
		set_process_status(sales_order, DELIVERABLE)
		return
	if process_status not in (DELIVERABLE, PARTIALLY_DELIVERED):
		frappe.throw(
			_("销售订单 {0} 当前流程状态为 {1}，不允许执行 CRM 发货。").format(
				sales_order.name, process_status or ""
			)
		)


def record_crm_repost_dependencies(delivery_note, created_since):
	reposts = get_crm_repost_dependencies(delivery_note, created_since=created_since, for_update=True)
	delivery_note.db_set("custom_crm_repost_ids", json.dumps(sorted({row.name for row in reposts})), update_modified=False)


def get_crm_repost_dependencies(delivery_note, *, created_since=None, for_update=False):
	"""Native item-based reposts have no voucher link; include the actual SLE pools."""
	if not delivery_note.get("company"):
		return []
	raw_ids = delivery_note.get("custom_crm_repost_ids") or "[]"
	tracked = frappe.parse_json(raw_ids) if isinstance(raw_ids, str) else raw_ids
	tracked = tracked if isinstance(tracked, list) else []
	vouchers = [("Delivery Note", delivery_note.name)]
	if delivery_note.get("custom_crm_stock_supplement"):
		vouchers.append(("Stock Entry", delivery_note.custom_crm_stock_supplement))
	voucher_conditions = " OR ".join("(voucher_type = %s AND voucher_no = %s)" for _ in vouchers)
	voucher_values = [value for voucher in vouchers for value in voucher]
	ledger_rows = frappe.db.sql(
		f"SELECT item_code, warehouse FROM `tabStock Ledger Entry` WHERE ({voucher_conditions})"
		+ (" FOR UPDATE" if for_update else ""), tuple(voucher_values), as_dict=True)
	pools = {(row.get("item_code"), row.get("warehouse")) for row in ledger_rows
		if row.get("item_code") and row.get("warehouse")}
	pools.update((row.get("item_code"), row.get("warehouse")) for row in delivery_note.get("items", [])
		if row.get("item_code") and row.get("warehouse"))
	scopes, scope_values = [f"({voucher_conditions})"], list(voucher_values)
	if pools:
		pair_conditions = " OR ".join("(item_code = %s AND warehouse = %s)" for _ in pools)
		scopes.append(f"(based_on = %s AND ({pair_conditions}))")
		scope_values.append("Item and Warehouse")
		scope_values.extend(value for pair in sorted(pools) for value in pair)
	statuses = "status IN ('Queued', 'In Progress', 'Failed')"
	status_values = []
	if created_since:
		statuses = f"({statuses} OR creation >= %s)"
		status_values.append(created_since)
	conditions = [f"(docstatus = 1 AND ({' OR '.join(scopes)}) AND {statuses})"]
	values = [delivery_note.company, *scope_values, *status_values]
	if tracked:
		conditions.append("name IN (" + ", ".join("%s" for _ in tracked) + ")")
		values.extend(tracked)
	rows = frappe.db.sql(
		"SELECT name, status, based_on, item_code, warehouse, voucher_type, voucher_no "
		"FROM `tabRepost Item Valuation` WHERE company = %s AND ("
		+ " OR ".join(conditions) + ") ORDER BY creation, name" + (" FOR UPDATE" if for_update else ""),
		tuple(values), as_dict=True)
	# A deleted/cancelled recorded job cannot be treated as completed valuation.
	found = {row.name for row in rows}
	rows.extend(frappe._dict(name=name, status="Missing") for name in tracked if name not in found)
	return rows


def get_crm_shipment_response(delivery_note, shipment_no, idempotent_replay=False, *, for_update=False):
	supplement_ids = [delivery_note.get("custom_crm_stock_supplement")] if delivery_note.get("custom_crm_stock_supplement") else []
	reposts = get_crm_repost_dependencies(delivery_note, for_update=for_update)
	valuation_failed = any(row.status in ("Failed", "Cancelled", "Missing") for row in reposts)
	valuation_pending = valuation_failed or any(row.status in ("Queued", "In Progress") for row in reposts)
	response = {
		"status": "success",
		"message": _("销售出库已在 ERP 提交并完成库存扣减。"),
		"name": delivery_note.name,
		"crm_shipment_no": shipment_no,
		"docstatus": delivery_note.docstatus,
		"stock_updated": delivery_note.docstatus == 1,
		"supplement_ids": supplement_ids,
		"source_fingerprint": delivery_note.get("custom_crm_source_fingerprint"),
		"valuation_pending": valuation_pending,
		"valuation_status": "failed" if valuation_failed else "pending" if valuation_pending else "complete",
		"repost_status": reposts,
		"valuation_policy": delivery_note.get("custom_crm_valuation_policy"),
		"timestamp": now(),
	}
	if idempotent_replay:
		response["message"] = _("CRM 发货单已处理，本次请求按幂等方式返回原销售出库。")
		response["idempotent_replay"] = True
	return response


def throw_crm_shipment_identity_conflict(message):
	frappe.response["error_code"] = "ERP_CRM_SHIPMENT_IDENTITY_CONFLICT"
	frappe.throw(
		f"ERP_CRM_SHIPMENT_IDENTITY_CONFLICT: {message}",
		exc=CRMShipmentIdentityConflict,
		title=_("CRM 发货单幂等编号冲突"),
	)


@contextmanager
def crm_shipment_lock(shipment_no):
	if getattr(frappe.db, "db_type", None) == "postgres":
		key = int.from_bytes(hashlib.sha256(str(shipment_no).encode()).digest()[:8], "big", signed=True)
		frappe.db.sql("SELECT pg_advisory_xact_lock(%s)", (key,))
		yield
		return
	if getattr(frappe.db, "db_type", None) != "mariadb":
		yield
		return

	digest = hashlib.sha256(str(shipment_no).encode()).hexdigest()
	lock_name = f"crm-shipment-{digest}"[:64]
	result = frappe.db.sql(
		"SELECT GET_LOCK(%s, %s)",
		(lock_name, CRM_SHIPMENT_LOCK_TIMEOUT),
	)
	if not result or cint(result[0][0]) != 1:
		frappe.throw(_("CRM 发货单号 {0} 正在处理中，请稍后重试。").format(shipment_no))

	released = False
	def release_lock():
		nonlocal released
		if not released:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
			released = True
	# Even when native submission raises, hold until the transaction has rolled back.
	frappe.db.after_commit.add(release_lock)
	frappe.db.after_rollback.add(release_lock)
	yield


@contextmanager
def crm_stock_pool_locks(rows):
	with ExitStack() as stack:
		for item_code, warehouse in sorted({(row["item_code"], row["warehouse"]) for row in rows}):
			stack.enter_context(crm_shipment_lock(f"stock:{item_code}:{warehouse}"))
		yield


def lock_sales_order_for_crm_shipment(sales_order_name):
	frappe.db.sql(
		"SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE",
		(sales_order_name,),
	)


def validate_sales_order_process_status(doc, method=None):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	validate_linked_sales_orders_in_statuses(
		doc,
		DRAFT_ALLOWED_STATUSES,
		_("以下销售订单状态不允许创建或保存销售出库草稿：<br>{0}"),
	)


def validate_sales_order_deliverable_before_submit(doc, method=None):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	preserve_crm_delivery_note_sales_order_pricing(doc)

	if is_delivery_note_marked_ready_to_deliver(doc):
		return

	validate_linked_sales_orders_in_statuses(
		doc,
		(DELIVERABLE, PARTIALLY_DELIVERED),
		_("以下销售订单未放行发货，不能提交销售出库：<br>{0}"),
	)


def preserve_crm_delivery_note_sales_order_pricing(delivery_note):
	"""Reapply order pricing after ERP validation and before CRM shipment submission."""
	if not delivery_note.get(CRM_SHIPMENT_FIELD):
		return

	sales_orders = get_linked_sales_orders(delivery_note)
	if len(sales_orders) != 1:
		frappe.throw(_("CRM 销售出库必须且只能关联一张销售订单。"))

	apply_sales_order_pricing_to_delivery_note(
		delivery_note,
		frappe.get_doc("Sales Order", sales_orders[0], for_update=True),
	)
	delivery_note.calculate_taxes_and_totals()


def set_pending_final_payment_before_insert(doc, method=None):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	for sales_order in get_linked_sales_orders(doc):
		process_status = frappe.db.get_value("Sales Order", sales_order, "custom_process_status")
		if process_status == PENDING_PRODUCTION:
			set_process_status(frappe.get_doc("Sales Order", sales_order), PENDING_FINAL_PAYMENT)


def rollback_pending_final_payment_on_trash(doc, method=None):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	if doc.docstatus != 0:
		return

	for sales_order in get_linked_sales_orders(doc):
		process_status = frappe.db.get_value("Sales Order", sales_order, "custom_process_status")
		if process_status != PENDING_FINAL_PAYMENT:
			continue

		if has_other_draft_delivery_note(sales_order, doc.name):
			continue

		set_process_status(frappe.get_doc("Sales Order", sales_order), PENDING_PRODUCTION)


def mark_sales_orders_completed_on_submit(doc, method=None):
	update_sales_orders_delivery_process_status(doc)


def update_sales_orders_delivery_process_status_on_cancel(doc, method=None):
	update_sales_orders_delivery_process_status(doc)


def update_sales_orders_delivery_process_status(doc):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	sales_orders = get_linked_sales_orders(doc)
	if not sales_orders:
		return

	updated_statuses = []
	for sales_order_name in sales_orders:
		sales_order = frappe.get_doc("Sales Order", sales_order_name)
		process_status = get_sales_order_delivery_process_status(sales_order_name)
		set_process_status(sales_order, process_status)
		updated_statuses.append(f"{sales_order_name}: {process_status}")
		if doc.get("custom_crm_historical_replay"):
			continue
		if doc.docstatus == 1 and not doc.get(CRM_SHIPMENT_FIELD):
			enqueue_crm_delivery_note_shipment_event(
				doc, sales_order_name, process_status
			)
		elif doc.docstatus == 2:
			enqueue_crm_delivery_note_cancellation_event(
				doc, sales_order_name, process_status
			)

	frappe.logger().info(
		f"Delivery Note {doc.name} updated Sales Order delivery process statuses: {', '.join(updated_statuses)}"
	)
	if doc.docstatus in (1, 2) and not doc.get("custom_crm_historical_replay"):
		enqueue_mes_delivery_note_status_callback(doc.name)


def enqueue_crm_delivery_note_shipment_event(doc, sales_order_name, process_status):
	items = get_delivery_note_shipment_items(doc, sales_order_name)
	if not items:
		return

	try:
		enqueue_sales_order_status_to_crm(
			sales_order_name=sales_order_name,
			external_status=CRM_STATUS_SHIPMENT_CREATED,
			triggered_status=process_status,
			trigger_event="delivery_note_submitted",
			delivery_note_name=doc.name,
			items=items,
			remark=get_delivery_note_shipment_remark(
				doc.name, sales_order_name, process_status
			),
		)
	except Exception:
		frappe.log_error(
			title="Failed to enqueue CRM Delivery Note shipment event",
			message=frappe.get_traceback(),
		)


def enqueue_crm_delivery_note_cancellation_event(doc, sales_order_name, process_status):
	items = get_delivery_note_shipment_items(doc, sales_order_name)
	if not items:
		return

	try:
		enqueue_sales_order_status_to_crm(
			sales_order_name=sales_order_name,
			external_status=CRM_STATUS_SHIPMENT_CANCELLED,
			triggered_status=process_status,
			trigger_event="delivery_note_cancelled",
			delivery_note_name=doc.name,
			items=items,
			remark=_("销售出库 {0} 已取消，ERP流程状态：{1}").format(
				doc.name, process_status
			),
		)
	except Exception:
		frappe.log_error(
			title="Failed to enqueue CRM Delivery Note cancellation event",
			message=frappe.get_traceback(),
		)


def get_delivery_note_shipment_remark(delivery_note_name, sales_order_name, process_status):
	if process_status == COMPLETED:
		return "全部发货"

	shipment_count = get_submitted_delivery_note_count(sales_order_name, delivery_note_name)
	return f"第{shipment_count}次部分发货"


def get_submitted_delivery_note_count(sales_order_name, current_delivery_note):
	rows = frappe.db.sql(
		"""
		SELECT COUNT(DISTINCT dn.name) AS delivery_note_count
		FROM `tabDelivery Note` dn
		INNER JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
		INNER JOIN `tabDelivery Note` current_dn ON current_dn.name = %s
		WHERE dn.docstatus = 1
			AND dni.against_sales_order = %s
			AND dn.creation <= current_dn.creation
		""",
		(current_delivery_note, sales_order_name),
		as_dict=True,
	)
	return max(int(rows[0].delivery_note_count if rows else 0), 1)


def get_delivery_note_shipment_items(doc, sales_order_name):
	qty_by_item = {}
	for row in doc.get("items", []):
		if row.get("against_sales_order") != sales_order_name:
			continue

		item_code = row.get("item_code")
		quantity = flt(row.get("stock_qty") or row.get("qty"))
		if not item_code or quantity <= 0:
			continue

		qty_by_item[item_code] = flt(qty_by_item.get(item_code)) + quantity

	return [
		{"externalItemId": item_code, "quantity": quantity}
		for item_code, quantity in sorted(qty_by_item.items())
	]


def enqueue_mes_delivery_note_status_callback(delivery_note_name):
	try:
		from mes_integration.mes_integration.delivery_note import (
			enqueue_delivery_note_status_callback,
		)
		if frappe.db.get_value("Delivery Note", delivery_note_name, "custom_crm_historical_replay"):
			return

		enqueue_delivery_note_status_callback(delivery_note_name)
	except ModuleNotFoundError as exc:
		if is_missing_mes_integration_error(exc):
			return
		raise
	except Exception:
		frappe.log_error(
			title="Failed to enqueue MES Delivery Note status callback",
			message=frappe.get_traceback(),
		)


def get_sales_order_delivery_process_status(sales_order_name):
	items = get_sales_order_item_quantities(sales_order_name)
	if not items:
		return DELIVERABLE

	delivered_qty_by_so_detail = get_delivered_qty_by_sales_order_item(sales_order_name)
	has_delivered_qty = False
	all_items_delivered = True
	for item in items:
		ordered_qty = item.stock_qty or item.qty or 0
		delivered_qty = delivered_qty_by_so_detail.get(item.name, 0)
		if delivered_qty > 0:
			has_delivered_qty = True
		if delivered_qty + 0.000001 < ordered_qty:
			all_items_delivered = False

	if all_items_delivered:
		return COMPLETED

	return PARTIALLY_DELIVERED if has_delivered_qty else DELIVERABLE


def get_sales_order_item_quantities(sales_order_name):
	return frappe.get_all(
		"Sales Order Item",
		filters={"parent": sales_order_name},
		fields=["name", "qty", "stock_qty"],
		order_by="idx asc",
	)


def get_delivered_qty_by_sales_order_item(sales_order_name):
	rows = frappe.db.sql(
		"""
		SELECT
			dni.so_detail,
			SUM(
				CASE
					WHEN dn.is_return = 1 THEN -ABS(COALESCE(dni.stock_qty, dni.qty, 0))
					ELSE COALESCE(dni.stock_qty, dni.qty, 0)
				END
			) AS delivered_qty
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_sales_order = %s
			AND dn.docstatus = 1
			AND IFNULL(dni.so_detail, '') != ''
		GROUP BY dni.so_detail
		""",
		(sales_order_name,),
		as_dict=True,
	)
	return {row.so_detail: max(row.delivered_qty or 0, 0) for row in rows}


def validate_linked_sales_orders_in_statuses(doc, allowed_statuses, message_template):
	sales_orders = get_linked_sales_orders(doc)
	if not sales_orders:
		return

	invalid_orders = frappe.get_all(
		"Sales Order",
		filters={
			"name": ["in", sales_orders],
			"custom_process_status": ["not in", allowed_statuses],
		},
		fields=["name", "custom_process_status"],
		order_by="name asc",
	)

	if invalid_orders:
		messages = [
			_("{0}: {1}").format(order.name, order.custom_process_status or "")
			for order in invalid_orders
		]
		frappe.throw(message_template.format("<br>".join(messages)))


def is_delivery_note_marked_ready_to_deliver(doc):
	if not frappe.db.has_column("Delivery Note", "custom_delivery_readiness_status"):
		return False

	return doc.get("custom_delivery_readiness_status") == "Ready to Deliver"


def get_linked_sales_orders(doc):
	return sorted(
		{
			item.against_sales_order
			for item in doc.get("items", [])
			if item.get("against_sales_order")
		}
	)


def has_other_draft_delivery_note(sales_order, current_delivery_note):
	return frappe.db.sql(
		"""
		SELECT dni.name
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_sales_order = %s
			AND dn.name != %s
			AND dn.docstatus = 0
		LIMIT 1
		""",
		(sales_order, current_delivery_note),
	)
