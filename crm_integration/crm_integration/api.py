from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

import frappe
from erpnext.controllers.accounts_controller import update_child_qty_rate
from erpnext.stock.get_item_details import get_item_details, get_item_warehouse_
from frappe import _
from frappe.utils import flt

from crm_integration.crm_integration.auth import validate_crm_api_user
from crm_integration.crm_integration.delivery_note import (
	CRM_ROW_FIELD,
	create_and_submit_delivery_note_from_crm,
	crm_shipment_fingerprint,
	get_crm_commercial_order_fingerprint,
	preview_delivery_note_from_crm,
)
from crm_integration.crm_integration.integration_log import create_crm_log, update_crm_log
from crm_integration.crm_integration.sales_order import (
	PENDING_CONFIRMATION,
	PENDING_DEPOSIT_CONFIRMATION,
	PENDING_PRODUCTION,
)
from crm_integration.crm_integration.sales_order_identity import (
	crm_sales_order_creation_lock,
	get_sales_order_names_by_crm_order_no,
)
from crm_integration.crm_integration.settings import (
	is_crm_integration_enabled,
	throw_crm_integration_disabled,
)

SALES_ORDER_UPDATE_ALLOWED_FIELDS = {
	"custom_crm_order_no",
	"custom_odt",
	"custom_remark",
	"delivery_date",
	"po_date",
	"po_no",
}
SALES_ORDER_ITEM_UPDATE_ALLOWED_FIELDS = {
	"qty",
	"rate",
	"new_item_code",
	"custom_product",
	"custom_specifications",
	"custom_version",
}
SALES_ORDER_ITEM_CORRECTION_FIELDS = {
	"new_item_code",
	"custom_product",
	"custom_specifications",
	"custom_version",
}
SALES_ORDER_ITEM_CORRECTION_ALLOWED_STATUSES = {
	PENDING_CONFIRMATION,
	PENDING_DEPOSIT_CONFIRMATION,
}
SALES_ORDER_ITEM_DOWNSTREAM_QTY_FIELDS = (
	"delivered_qty",
	"work_order_qty",
	"produced_qty",
	"picked_qty",
	"planned_qty",
	"production_plan_qty",
	"requested_qty",
	"stock_reserved_qty",
)
SALES_ORDER_ITEM_REFRESH_FIELDS = (
	"item_name",
	"description",
	"image",
	"customer_item_code",
	"item_group",
	"brand",
	"stock_uom",
	"uom",
	"conversion_factor",
	"price_list_rate",
	"base_price_list_rate",
	"rate",
	"base_rate",
	"amount",
	"base_amount",
	"net_rate",
	"base_net_rate",
	"net_amount",
	"base_net_amount",
	"margin_type",
	"margin_rate_or_amount",
	"rate_with_margin",
	"discount_percentage",
	"discount_amount",
	"pricing_rules",
	"item_tax_template",
	"item_tax_rate",
	"weight_per_unit",
	"total_weight",
	"weight_uom",
	"valuation_rate",
	"gross_profit",
	"grant_commission",
	"is_stock_item",
	"bom_no",
)
CRM_ORDER_SOURCE_ITEM_FIELDS = {
	"id", "itemCode", "version", "unit", "quantity", "unitPrice", "supplyPrice", "vatAmount", "lineNo",
}
CRM_PIECE_UNITS = {"件", "个", "pieza", "piezas", "pcs", "nos"}
CRM_MISSING_ITEMS_EVENT = "Sales Order Complete Missing CRM Items"


def _crm_number(value):
	if value is None or value == "" or isinstance(value, bool):
		return None
	try:
		number = float(value)
		return number if isfinite(number) else None
	except (ValueError, TypeError):
		return None


def _crm_piece_unit(value):
	parts = str(value or "").replace("：", ":").split(":")
	return bool(parts) and all(part.strip().lower() in CRM_PIECE_UNITS for part in parts)


def _crm_source_matches_row(source, row):
	if (source.get("itemCode") != row.get("item_code")
		or str(source.get("version") or "") != str(row.get("custom_version") or "")
		or source["qty"] != _crm_number(row.get("qty"))
		or source["rate"] != _crm_number(row.get("net_rate") if row.get("net_rate") is not None else row.get("rate"))):
		return False
	unit = source.get("unit")
	if unit == row.get("uom"):
		return True
	# Unknown labels require explicit Item UOM evidence; never silently map 台/Box to Nos.
	item = frappe.get_cached_doc("Item", row.get("item_code"))
	factors = {item.get("stock_uom"): 1}
	factors.update({u.get("uom"): _crm_number(u.get("conversion_factor")) for u in item.get("uoms", [])})
	if unit not in factors and _crm_piece_unit(unit) and _crm_piece_unit(row.get("uom")):
		return _crm_number(row.get("conversion_factor")) == 1
	return bool(factors.get(unit)) and factors.get(unit) == _crm_number(row.get("conversion_factor"))


def plan_missing_crm_sales_order_items(doc, source_items, *, historical_replay=False):
	"""Read-only proof that a full CRM manifest contains genuinely additional SO rows."""
	blockers, added = [], []
	fingerprint = crm_shipment_fingerprint(dict(order_fingerprint=get_crm_commercial_order_fingerprint(doc),
		source_items=source_items, historical_replay=historical_replay,
		row_bindings=[(row.get("name"), row.get(CRM_ROW_FIELD)) for row in doc.get("items", [])]))
	result = dict(can_complete=False, blockers=blockers, added_items=added, completion_fingerprint=fingerprint)
	if not isinstance(source_items, list) or not source_items:
		blockers.append(_("补全必须提供完整的 CRM items manifest。"))
		return result
	sources, ids = [], set()
	for index, raw in enumerate(source_items, 1):
		if not isinstance(raw, dict) or set(raw) - CRM_ORDER_SOURCE_ITEM_FIELDS:
			blockers.append(_("CRM manifest 第 {0} 行包含不支持的字段或不是对象。").format(index))
			continue
		source = dict(raw)
		identity = str(raw.get("id") or "")
		qty = _crm_number(raw.get("quantity"))
		supply_price = _crm_number(raw.get("supplyPrice"))
		rate = supply_price if supply_price is not None and supply_price > 0 else _crm_number(raw.get("unitPrice"))
		if not identity or identity in ids or not raw.get("itemCode") or not raw.get("unit"):
			blockers.append(_("CRM manifest 第 {0} 行缺少唯一 id、itemCode、unit 或 id 重复。").format(index))
		if qty is None or qty <= 0:
			blockers.append(_("CRM manifest 第 {0} 行 quantity 必须为正数。").format(index))
		if rate is None or rate < 0:
			blockers.append(_("CRM manifest 第 {0} 行缺少有效 price；缺失价格不能视为零。").format(index))
		if not isinstance(raw.get("version"), str) or len(raw["version"]) > 64:
			blockers.append(_("CRM manifest 第 {0} 行 version 必须明确提供字符串（无版本用空字符串），且不能超过 64 字符。").format(index))
		ids.add(identity)
		source.update(identity=identity, qty=qty, rate=rate)
		sources.append(source)
	if blockers:
		return result

	used = set()
	unbound = []
	for row in doc.get("items", []):
		identity = str(row.get(CRM_ROW_FIELD) or "")
		if not identity:
			unbound.append(row)
		elif identity not in ids or identity in used:
			blockers.append(_("现有 ERP 明细 {0} 的 CRM 行绑定缺失或重复，禁止追加替代行。").format(row.get("name")))
		else:
			source = next(source for source in sources if source["identity"] == identity)
			if not _crm_source_matches_row(source, row):
				blockers.append(_("现有 ERP 明细 {0} 虽已绑定 CRM 行，但编码、版本、数量、价格或单位已冲突，禁止追加替代行。").format(row.get("name")))
			used.add(identity)
	for row in unbound:
		matches = [source for source in sources if source["identity"] not in used and _crm_source_matches_row(source, row)]
		if len(matches) != 1:
			blockers.append(_("现有 ERP 明细 {0} 无法唯一对应 CRM manifest，禁止追加替代行或重复数量。").format(row.get("name")))
		else:
			used.add(matches[0]["identity"])
	remaining = [source for source in sources if source["identity"] not in used]
	if not remaining:
		return result
	if historical_replay:
		from crm_integration.crm_integration.delivery_note import get_crm_historical_order_completion_blockers
		blockers.extend(get_crm_historical_order_completion_blockers())
	if doc.get("docstatus") != 1 or doc.get("status") in {"Closed", "On Hold", "Cancelled", "Completed"}:
		blockers.append(_("销售订单当前原生状态不允许补全明细。"))
	try:
		validate_sales_order_update_status(doc)
	except frappe.ValidationError as exc:
		blockers.append(str(exc))
	keys = [(s.get("itemCode"), s.get("version"), s.get("unit"), s["qty"], s["rate"]) for s in remaining]
	if len(keys) != len(set(keys)):
		blockers.append(_("CRM 待补明细存在重复、歧义行，须先建立唯一现有行绑定。"))
	if any(flt(doc.get(field)) for field in ("discount_amount", "base_discount_amount", "additional_discount_percentage")):
		blockers.append(_("订单 header discount 会重新分摊既有行有效价格，不能自动补全。"))
	if doc.get("taxes"):
		blockers.append(_("订单已有税表（含 Actual 税额），无法证明补行后原有效价格与税额不变。"))
	for source in remaining:
		code = source["itemCode"]
		vat = _crm_number(source.get("vatAmount"))
		if vat is None or vat != 0:
			blockers.append(_("CRM 行 {0} VAT 缺失或非零，无法无歧义沿用原生税务处理。").format(source["identity"]))
		try:
			item = frappe.get_cached_doc("Item", code)
		except frappe.DoesNotExistError:
			blockers.append(_("补入物料 {0} 不存在，须先维护原生 Item。").format(code))
			continue
		if item.get("disabled") or not item.get("is_stock_item") or not item.get("is_sales_item"):
			blockers.append(_("补入物料 {0} 必须是启用的库存销售 Item。").format(code))
		if item.get("taxes"):
			blockers.append(_("补入物料 {0} 存在 Item 税务模板，无法证明与 CRM VAT 一致。").format(code))
		factors = {item.get("stock_uom"): 1}
		factors.update({u.get("uom"): _crm_number(u.get("conversion_factor")) for u in item.get("uoms", [])})
		uom = source.get("unit")
		if uom not in factors and _crm_piece_unit(uom):
			candidates = [unit for unit, factor in factors.items() if _crm_piece_unit(unit) and factor == 1]
			uom = item.get("stock_uom") if item.get("stock_uom") in candidates else (candidates[0] if len(candidates) == 1 else None)
		factor = factors.get(uom)
		if not factor or factor <= 0:
			blockers.append(_("物料 {0} 的 CRM UOM {1} 缺少原生 Item 换算证明。").format(code, source.get("unit")))
		defaults = [value.get("default_warehouse") for value in item.get("item_defaults", [])
			if value.get("company") == doc.get("company") and value.get("default_warehouse")]
		warehouse = doc.get("set_warehouse") or (defaults[0] if len(defaults) == 1 else None)
		if not warehouse:
			blockers.append(_("物料 {0} 缺少唯一公司 ItemDefault 或 SO header warehouse。").format(code))
		else:
			values = frappe.db.get_value("Warehouse", warehouse, ["company", "disabled", "is_group"], as_dict=True)
			if not values or values.get("company") != doc.get("company") or values.get("disabled") or values.get("is_group"):
				blockers.append(_("物料 {0} 的 warehouse 不存在、停用、为组仓库或公司不一致。").format(code))
			elif get_item_warehouse_(doc, item, overwrite_warehouse=True) != warehouse:
				blockers.append(_("物料 {0} 的原生仓库默认值与公司 ItemDefault/SO header 不一致。").format(code))
		if not doc.get("delivery_date"):
			blockers.append(_("SO 缺少 delivery_date，不能为补行编造日期。"))
		added.append(dict(item_code=code, custom_version=source.get("version") or "", crm_order_item_id=source["identity"],
			qty=source["qty"], rate=source["rate"], uom=uom, conversion_factor=factor,
			warehouse=warehouse, delivery_date=doc.get("delivery_date"), description=item.get("description") or item.get("item_name")))
	resolved_keys = [(row["item_code"], row["custom_version"], row["uom"], row["conversion_factor"], row["qty"], row["rate"])
		for row in added]
	if len(resolved_keys) != len(set(resolved_keys)):
		blockers.append(_("CRM 待补明细换算为原生 UOM 后存在重复、歧义行。"))
	result["can_complete"] = bool(added) and not blockers
	result["completion_fingerprint"] = crm_shipment_fingerprint(dict(order_and_manifest=fingerprint, added_items=added))
	return result


@frappe.whitelist(methods=["POST"])
def create_and_submit_sales_order(sales_order=None):
	"""Create a Sales Order from an external system and submit it immediately."""
	validate_crm_api_user()
	payload = get_request_payload(sales_order)
	validate_sales_order_payload(payload)
	if not is_crm_integration_enabled(payload.get("company")):
		throw_crm_integration_disabled(payload.get("company"))

	with crm_sales_order_creation_lock(payload["custom_crm_order_no"]):
		existing_response = get_existing_sales_order_response(payload)
		if existing_response:
			log_existing_sales_order_request(payload, existing_response)
			return existing_response

		return create_sales_order(payload)


def create_sales_order(payload, resubmission=False):
	if payload.get("crm_historical_replay"):
		from crm_integration.crm_integration.delivery_note import get_crm_historical_parent_creation_blockers

		blockers = get_crm_historical_parent_creation_blockers(payload.get("company"))
		if blockers:
			frappe.throw("\n".join(blockers))
	# The historical replay marker is transport evidence, never a business field.
	payload = {key: value for key, value in payload.items() if key != "crm_historical_replay"}
	ensure_sales_persons_exist(payload)
	crm_log = create_crm_log(
		direction="Inbound",
		event="Sales Order Create And Submit",
		status="Pending",
		source="CRM",
		request_url=get_request_url(),
		request_payload=payload,
	)

	try:
		doc = frappe.get_doc(payload)
		doc.custom_crm_production_started = 0
		doc.custom_crm_production_milestone = "NOT_STARTED"
		if resubmission:
			doc.flags.crm_resubmission = True
		doc.insert(ignore_permissions=True)
		doc.flags.crm_order_operation = True
		preserve_explicit_zero_rates(doc, payload)
		doc.submit()

		if doc.get("custom_process_status") != PENDING_DEPOSIT_CONFIRMATION:
			doc.db_set("custom_process_status", PENDING_DEPOSIT_CONFIRMATION, update_modified=True)
			doc.custom_process_status = PENDING_DEPOSIT_CONFIRMATION

		response = {
			"status": "success",
			"message": _("销售订单已创建并提交。"),
			"name": doc.name,
			"docstatus": doc.docstatus,
			"process_status": doc.get("custom_process_status"),
		}

		update_crm_log(
			crm_log,
			status="Success",
			reference_doctype="Sales Order",
			reference_name=doc.name,
			response_payload=response,
			http_status_code=200,
		)
		return response
	except Exception:
		update_crm_log(
			crm_log,
			status="Failed",
			error_message=frappe.get_traceback(),
			http_status_code=500,
		)
		raise


def get_existing_sales_order_response(payload):
	crm_order_no = payload["custom_crm_order_no"]
	sales_order_names = get_sales_order_names_by_crm_order_no(crm_order_no, limit=0)
	if not sales_order_names:
		return None
	active_docs = [doc for doc in (frappe.get_doc("Sales Order", name) for name in sales_order_names) if doc.docstatus == 1]
	if len(active_docs) > 1:
		frappe.throw(_("CRM 销售订单号 {0} 已对应多笔有效销售订单，请先处理重复数据。").format(crm_order_no))
	if not active_docs:
		frappe.throw(_("CRM 销售订单号 {0} 只有历史或草稿单；重新提交请使用专用接口。").format(crm_order_no))
	doc = active_docs[0]
	if doc.get("company") != payload.get("company"):
		frappe.throw(_("CRM 销售订单号 {0} 已被其他公司使用。").format(crm_order_no))
	if doc.docstatus != 1:
		frappe.throw(
			_("CRM 销售订单号 {0} 已存在，但对应销售订单不是已提交状态。").format(crm_order_no)
		)

	return {
		"status": "success",
		"message": _("销售订单已存在，本次请求按幂等方式返回原单据。"),
		"name": doc.name,
		"docstatus": doc.docstatus,
		"process_status": doc.get("custom_process_status"),
		"idempotent_replay": True,
	}


def log_existing_sales_order_request(payload, response):
	create_crm_log(
		direction="Inbound",
		event="Sales Order Create And Submit",
		status="Success",
		source="CRM",
		reference_doctype="Sales Order",
		reference_name=response["name"],
		request_url=get_request_url(),
		request_payload=payload,
		response_payload=response,
		http_status_code=200,
	)


def preserve_explicit_zero_rates(doc, payload):
	"""Keep an explicitly supplied zero rate from being replaced by price lookup."""
	for payload_item, item in zip(payload.get("items") or [], doc.get("items") or []):
		if "rate" not in payload_item or payload_item.get("rate") is None:
			continue
		if flt(payload_item.get("rate")) != 0:
			continue

		item.rate = 0
		item.price_list_rate = 0
		item.discount_amount = 0
		item.pricing_rules = None

	doc.flags.ignore_pricing_rule = True
	doc.calculate_taxes_and_totals()
	doc.save(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def update_sales_order_by_crm_order_no(sales_order: Any = None):
	"""Update an existing Sales Order using its CRM order number."""
	validate_crm_api_user()
	payload = get_request_payload(sales_order)
	validate_sales_order_update_payload(payload)
	if payload.get("mode") == "complete_missing_items":
		return complete_missing_crm_sales_order_items(payload)

	crm_order_no = payload["custom_crm_order_no"]
	sales_order_names = get_sales_order_names_by_crm_order_no(crm_order_no, docstatus=1)
	if not sales_order_names:
		frappe.throw(_("未找到 CRM 销售订单号 {0} 对应的销售订单。").format(crm_order_no))
	if len(sales_order_names) > 1:
		frappe.throw(_("CRM 销售订单号 {0} 对应多笔有效销售订单，无法安全修改。").format(crm_order_no))

	doc = frappe.get_doc("Sales Order", sales_order_names[0])
	if not is_crm_integration_enabled(doc.get("company")):
		throw_crm_integration_disabled(doc.get("company"))
	validate_sales_order_update_status(doc)
	from crm_integration.crm_integration.order_revision import eligibility, linked_facts

	code, reason = eligibility(doc, linked_facts(doc))
	if code:
		frappe.throw(_("{0}: {1}").format(code, reason))
	update_sales_order_items(doc, payload.get("items"))
	for field, value in payload.items():
		if field in SALES_ORDER_UPDATE_ALLOWED_FIELDS:
			doc.set(field, value)

	if payload.get("items"):
		doc.calculate_taxes_and_totals()
	if doc.docstatus == 1:
		doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
	return {
		"status": "success",
		"message": _("销售订单已更新。"),
		"name": doc.name,
		"crm_order_no": doc.custom_crm_order_no,
		"docstatus": doc.docstatus,
	}


def _crm_document_snapshot(doc):
	return doc.as_dict(no_private_properties=True) if callable(getattr(doc, "as_dict", None)) else dict(doc)


@frappe.whitelist(methods=["POST"])
def get_sales_order_completion_receipt(sales_order: Any = None):
	"""Read only: recover a completion result without authorizing or applying a frozen request."""
	validate_crm_api_user()
	payload = get_request_payload(sales_order)
	validate_sales_order_update_payload(payload)
	if payload.get("mode") != "complete_missing_items":
		frappe.throw(_("查询补全回执必须使用 complete_missing_items mode。"))
	receipt = get_crm_missing_items_completion_receipt(payload)
	return dict(status="found" if receipt is not None else "missing", receipt=receipt)


def get_crm_missing_items_completion_receipt(payload, *, for_update=False):
	"""Recover a committed native completion after a transport timeout, before mutable order checks."""
	if for_update:
		logs = frappe.db.sql("SELECT reference_name, request_payload, response_payload FROM `tabCRM Integration Log` "
			"WHERE event=%s AND trace_id=%s AND status='Success' AND direction='Inbound' "
			"AND source='CRM' AND reference_doctype='Sales Order' LIMIT 2 FOR UPDATE",
			(CRM_MISSING_ITEMS_EVENT, payload["completion_fingerprint"]), as_dict=True)
	else:
		logs = frappe.get_all("CRM Integration Log", filters={"event": CRM_MISSING_ITEMS_EVENT,
			"trace_id": payload["completion_fingerprint"], "status": "Success", "direction": "Inbound",
			"source": "CRM", "reference_doctype": "Sales Order"},
			fields=["reference_name", "request_payload", "response_payload"], limit_page_length=2)
	if not logs:
		return None
	if len(logs) != 1:
		frappe.throw(_("补全 completion_fingerprint 存在重复回执，须人工核对。"))
	log = logs[0]
	stored_request = frappe.parse_json(log.get("request_payload"))
	response = frappe.parse_json(log.get("response_payload"))
	if (not isinstance(stored_request, dict) or not isinstance(response, dict)
		or crm_shipment_fingerprint(stored_request) != crm_shipment_fingerprint(payload)
		or response.get("name") != log.get("reference_name")
		or response.get("crm_order_no") != payload["custom_crm_order_no"]):
		frappe.throw(_("补全回执与完整 CRM manifest 或订单身份冲突，禁止重复写入。"))
	return response


def complete_missing_crm_sales_order_items(payload):
	"""Append only proven missing rows through ERPNext's native submitted-order editor."""
	crm_order_no = payload["custom_crm_order_no"]
	with crm_sales_order_creation_lock(crm_order_no):
		receipt = get_crm_missing_items_completion_receipt(payload, for_update=True)
		if receipt is not None:
			return receipt
		names = get_sales_order_names_by_crm_order_no(crm_order_no, docstatus=1)
		if len(names) != 1:
			frappe.throw(_("CRM 销售订单号 {0} 必须唯一对应一笔有效销售订单。").format(crm_order_no))
		frappe.db.sql("SELECT name FROM `tabSales Order` WHERE name=%s FOR UPDATE", (names[0],))
		frappe.db.sql("SELECT name FROM `tabSales Order Item` WHERE parent=%s ORDER BY name FOR UPDATE", (names[0],))
		doc = frappe.get_doc("Sales Order", names[0], for_update=True)
		if doc.get("custom_crm_order_no") != crm_order_no:
			frappe.throw(_("CRM 订单身份在加锁前已变化，请重新预览。"))
		if not is_crm_integration_enabled(doc.get("company")):
			throw_crm_integration_disabled(doc.get("company"))
		validate_sales_order_update_status(doc)
		plan = plan_missing_crm_sales_order_items(doc, payload["items"], historical_replay=bool(payload.get("historical_replay")))
		if payload["completion_fingerprint"] != plan["completion_fingerprint"]:
			frappe.throw(_("订单或完整 CRM manifest 已变化，请重新预览后补全。"))
		if not plan["can_complete"]:
			frappe.throw("\n".join(plan["blockers"]))
		savepoint = "crm_missing_items_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(savepoint)
		try:
			added_rows = _apply_missing_crm_sales_order_items(doc, plan) if plan["added_items"] else []
			response = dict(status="success", message=_("销售订单缺失明细已补全。"), name=doc.name,
				crm_order_no=crm_order_no, docstatus=doc.docstatus, added_items=added_rows,
				order_fingerprint=get_crm_commercial_order_fingerprint(doc))
			response = frappe.parse_json(frappe.as_json(response))
			log = create_crm_log(direction="Inbound", event=CRM_MISSING_ITEMS_EVENT, status="Success", source="CRM",
				reference_doctype="Sales Order", reference_name=doc.name, trace_id=payload["completion_fingerprint"],
				request_url=get_request_url(), request_payload=payload, response_payload=response, http_status_code=200)
			if not log:
				frappe.throw(_("补全回执保存失败，原生补行已中止；请勿将本次操作视为成功。"))
			return response
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise


def _apply_missing_crm_sales_order_items(doc, plan):
	existing = {row.get("name"): _crm_document_snapshot(row) for row in doc.get("items", [])}
	before_fingerprint = get_crm_commercial_order_fingerprint(doc)
	# Native editing treats omitted rows as deletions. Pass every old row without any business changes.
	trans_items = [dict(values, docname=name) for name, values in existing.items()]
	trans_items.extend(plan["added_items"])
	update_child_qty_rate(parent_doctype="Sales Order", trans_items=frappe.as_json(trans_items),
		parent_doctype_name=doc.name, child_docname="items")
	doc.reload()
	current = {row.get("name"): row for row in doc.get("items", [])}
	for name, values in existing.items():
		if name not in current or any(current[name].get(field) != value for field, value in values.items()
			if field not in {"modified", "modified_by", "idx"}):
			frappe.throw(_("原生补行改变了既有明细 {0}，已中止补全。").format(name))
	commercial_snapshot = _crm_document_snapshot(doc)
	commercial_snapshot["items"] = [row for row in doc.get("items", []) if row.get("name") in existing]
	if get_crm_commercial_order_fingerprint(frappe._dict(commercial_snapshot)) != before_fingerprint:
		frappe.throw(_("原生补行改变了既有订单商业或税务条件，已中止补全。"))
	new_rows = [row for row in doc.get("items", []) if row.get("name") not in existing]
	if len(new_rows) != len(plan["added_items"]):
		frappe.throw(_("原生补入行数与已预览方案不一致。"))
	added_rows = []
	for row, expected in zip(new_rows, plan["added_items"]):
		for field in ("item_code", "qty", "rate", "uom", "conversion_factor", "warehouse", "delivery_date"):
			if str(row.get(field)) != str(expected.get(field)) and row.get(field) != expected.get(field):
				frappe.throw(_("补入行 {0} 的 {1} 与已预览方案不一致。").format(row.get("name"), field))
		if row.get("net_rate") is not None and flt(row.get("net_rate")) != expected["rate"]:
			frappe.throw(_("补入行有效 net_rate 与 CRM price 不一致。"))
		values = {CRM_ROW_FIELD: expected["crm_order_item_id"], "custom_version": expected["custom_version"]}
		frappe.db.set_value("Sales Order Item", row.get("name"), values, update_modified=False)
		row.update(values)
		added_rows.append(dict(expected, name=row.get("name")))
	return added_rows


def update_sales_order_items(doc, items):
	if not items:
		return
	if isinstance(items, str):
		items = frappe.parse_json(items)
	if not isinstance(items, list):
		frappe.throw(_("items 必须是数组。"))

	rows_by_name = {row.name: row for row in doc.items}
	rows_by_item_code = {row.item_code: row for row in doc.items if row.item_code}

	for item in items:
		if not isinstance(item, dict):
			frappe.throw(_("items 中的每一项必须是对象。"))

		if item.get("name"):
			row = rows_by_name.get(item.get("name"))
		else:
			row = rows_by_item_code.get(item.get("item_code"))
		if not row:
			identifier = item.get("name") or item.get("item_code") or _("未提供")
			frappe.throw(_("销售订单中未找到明细 {0}。").format(identifier))

		if item.get("name") and item.get("item_code") and item.get("item_code") != row.item_code:
			frappe.throw(
				_("明细 {0} 当前物料为 {1}，与请求中的 item_code {2} 不一致。").format(
					row.name, row.item_code, item.get("item_code")
				)
			)

		fields = set(item) - {"name", "item_code"}
		unsupported_fields = fields - SALES_ORDER_ITEM_UPDATE_ALLOWED_FIELDS
		if unsupported_fields:
			frappe.throw(_("不支持修改销售订单明细字段：{0}。").format(", ".join(sorted(unsupported_fields))))
		if not fields:
			frappe.throw(_("明细 {0} 未提供需要修改的字段。").format(row.item_code or row.name))

		correction_fields = fields & SALES_ORDER_ITEM_CORRECTION_FIELDS
		if correction_fields:
			validate_sales_order_item_correction(doc, row, item)

		if "new_item_code" in fields:
			replace_sales_order_item(doc, row, item.get("new_item_code"))

		for field in fields - {"new_item_code"}:
			row.set(field, item[field])

		if "rate" in fields and flt(item.get("rate")) == 0:
			row.price_list_rate = 0
			row.discount_amount = 0
			row.pricing_rules = None


def validate_sales_order_item_correction(doc, row, item):
	if not item.get("name"):
		frappe.throw(_("更正销售订单物料或版本时必须传明细 name，不能只按 item_code 定位。"))

	process_status = doc.get("custom_process_status")
	if process_status not in SALES_ORDER_ITEM_CORRECTION_ALLOWED_STATUSES:
		frappe.throw(
			_("销售订单 {0} 当前状态为 {1}，物料或版本只能在推送 MES 前更正。").format(
				doc.name, process_status or _("未设置")
			)
		)

	linked_qty_fields = [
		fieldname
		for fieldname in SALES_ORDER_ITEM_DOWNSTREAM_QTY_FIELDS
		if flt(row.get(fieldname)) != 0
	]
	if linked_qty_fields:
		frappe.throw(
			_("销售订单明细 {0} 已产生下游数量，不能替换物料或版本：{1}。").format(
				row.name, ", ".join(linked_qty_fields)
			)
		)

	if "new_item_code" in item and not item.get("new_item_code"):
		frappe.throw(_("new_item_code 不能为空。"))
	if "custom_version" in item and len(str(item.get("custom_version") or "")) > 64:
		frappe.throw(_("custom_version 最长为 64 个字符。"))


def replace_sales_order_item(doc, row, new_item_code):
	if new_item_code == row.item_code:
		return

	preserved_values = {
		"qty": row.get("qty"),
		"delivery_date": row.get("delivery_date"),
		"warehouse": row.get("warehouse"),
	}
	row.item_code = new_item_code
	for fieldname in SALES_ORDER_ITEM_REFRESH_FIELDS:
		if row.meta.get_field(fieldname):
			row.set(fieldname, None)

	parent_values = {fieldname: doc.get(fieldname) for fieldname in doc.meta.get_valid_columns()}
	parent_values["document_type"] = row.doctype
	ctx = frappe._dict(parent_values)
	ctx.update(row.as_dict())
	ctx.update(
		{
			"doctype": doc.doctype,
			"name": doc.name,
			"child_doctype": row.doctype,
			"child_docname": row.name,
			"ignore_pricing_rule": doc.get("ignore_pricing_rule") or 0,
		}
	)
	details = get_item_details(ctx, doc, for_validate=True, overwrite_warehouse=False)
	for fieldname, value in details.items():
		if row.meta.get_field(fieldname) and value is not None:
			row.set(fieldname, value)

	for fieldname, value in preserved_values.items():
		row.set(fieldname, value)


def validate_sales_order_update_status(doc):
	allowed_statuses = {
		PENDING_CONFIRMATION,
		PENDING_DEPOSIT_CONFIRMATION,
		PENDING_PRODUCTION,
	}
	process_status = doc.get("custom_process_status")
	if process_status and process_status not in allowed_statuses:
		frappe.throw(
			_("销售订单 {0} 当前状态为 {1}，只有生产中及之前的状态允许修改。").format(
				doc.name, process_status
			)
		)


def get_request_payload(sales_order=None):
	if isinstance(sales_order, str):
		return frappe.parse_json(sales_order)

	if isinstance(sales_order, dict):
		return sales_order

	if frappe.request and frappe.request.is_json:
		request_json = frappe.request.get_json(silent=True) or {}
		if request_json.get("sales_order"):
			return request_json.get("sales_order")
		return request_json

	return dict(frappe.form_dict.get("sales_order") or frappe.form_dict)


def validate_sales_order_payload(payload):
	if not isinstance(payload, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))

	payload.setdefault("doctype", "Sales Order")
	if payload.get("doctype") != "Sales Order":
		frappe.throw(_("该接口只支持创建 Sales Order。"))

	if payload.get("docstatus"):
		frappe.throw(_("请求体不能直接传 docstatus，请由接口自动提交销售订单。"))
	if {"custom_crm_production_started", "custom_crm_production_milestone"} & set(payload):
		frappe.throw(_("生产里程碑由 ERP 管理，建单请求不能直接设置。"))

	if not payload.get("customer"):
		frappe.throw(_("缺少必填字段：customer"))

	if not payload.get("custom_crm_order_no"):
		frappe.throw(_("缺少必填字段：custom_crm_order_no"))

	if not payload.get("items"):
		frappe.throw(_("缺少销售订单明细：items"))
	for item in payload["items"]:
		if isinstance(item, dict):
			validate_crm_item_extra_fields(item)


def validate_crm_item_extra_fields(item):
	if "custom_item_tax_amount" in item:
		value = item["custom_item_tax_amount"]
		if isinstance(value, bool):
			frappe.throw(_("custom_item_tax_amount 必须是有限的非负金额。"))
		try:
			amount = Decimal(str(value))
		except (InvalidOperation, TypeError, ValueError):
			frappe.throw(_("custom_item_tax_amount 必须是有限的非负金额。"))
		if not amount.is_finite() or amount < 0:
			frappe.throw(_("custom_item_tax_amount 必须是有限的非负金额。"))
	if "custom_mes_route_scenario" in item:
		value = item["custom_mes_route_scenario"]
		if value is not None and (not isinstance(value, str) or len(value) > 64):
			frappe.throw(_("custom_mes_route_scenario 必须是不超过 64 个字符的字符串。"))


def validate_sales_order_update_payload(payload: Any):
	if not isinstance(payload, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))

	if payload.get("doctype") and payload["doctype"] != "Sales Order":
		frappe.throw(_("该接口只支持修改 Sales Order。"))
	if payload.get("name"):
		frappe.throw(_("请使用 custom_crm_order_no 定位销售订单，不要传 name。"))
	if payload.get("docstatus") is not None:
		frappe.throw(_("不允许通过该接口修改 docstatus。"))
	if not payload.get("custom_crm_order_no"):
		frappe.throw(_("缺少必填字段：custom_crm_order_no"))
	if payload.get("mode") == "complete_missing_items":
		allowed = {"doctype", "custom_crm_order_no", "mode", "completion_fingerprint", "items", "historical_replay"}
		unsupported = set(payload) - allowed
		if unsupported:
			frappe.throw(_("补全明细不支持字段：{0}。").format(", ".join(sorted(unsupported))))
		if not payload.get("completion_fingerprint"):
			frappe.throw(_("补全必须提供 completion_fingerprint，请先预览。"))
		if "historical_replay" in payload and not isinstance(payload["historical_replay"], bool):
			frappe.throw(_("historical_replay 必须是布尔值。"))
		if not isinstance(payload.get("items"), list) or not payload["items"]:
			frappe.throw(_("补全必须提供完整 CRM items manifest。"))
		for row in payload["items"]:
			if not isinstance(row, dict) or set(row) - CRM_ORDER_SOURCE_ITEM_FIELDS:
				frappe.throw(_("完整 CRM items manifest 包含不支持的行字段。"))
		return

	unsupported_fields = set(payload) - SALES_ORDER_UPDATE_ALLOWED_FIELDS - {
		"doctype",
		"items",
	}
	if unsupported_fields:
		frappe.throw(
			_("不支持修改销售订单字段：{0}。").format(", ".join(sorted(unsupported_fields)))
		)


def ensure_sales_persons_exist(payload):
	"""Ensure every Sales Person referenced in sales_team exists in ERP.

	If a Sales Person does not exist, create it automatically as a leaf node.
	"""
	sales_team = payload.get("sales_team")
	if not sales_team:
		return

	for row in sales_team:
		person_name = row.get("sales_person")
		if not person_name:
			continue

		if not frappe.db.exists("Sales Person", {"sales_person_name": person_name}):
			frappe.get_doc(
				{
					"doctype": "Sales Person",
					"sales_person_name": person_name,
					"is_group": 0,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)


def get_request_url():
	if not getattr(frappe.local, "request", None):
		return None

	return getattr(frappe.request, "url", None)


@frappe.whitelist(methods=["POST"])
def preview_delivery_note(shipment=None):
	"""Read-only native shipment mappings, shortages, blockers and fingerprints."""
	validate_crm_api_user()
	return preview_delivery_note_from_crm(get_shipment_request_payload(shipment))


@frappe.whitelist(methods=["POST"])
def create_and_submit_delivery_note(shipment=None):
	"""Create or submit the ERP Delivery Note for one CRM shipment."""
	validate_crm_api_user()
	payload = get_shipment_request_payload(shipment)
	return create_and_submit_delivery_note_from_crm(payload)


def get_shipment_request_payload(shipment=None):
	if isinstance(shipment, str):
		return frappe.parse_json(shipment)

	if isinstance(shipment, dict):
		return shipment

	if frappe.request and frappe.request.is_json:
		request_json = frappe.request.get_json(silent=True) or {}
		wrapped_payload = request_json.get("shipment")
		if isinstance(wrapped_payload, str):
			return frappe.parse_json(wrapped_payload)
		if isinstance(wrapped_payload, dict):
			return wrapped_payload
		return request_json

	wrapped_payload = frappe.form_dict.get("shipment")
	if isinstance(wrapped_payload, str):
		return frappe.parse_json(wrapped_payload)
	if isinstance(wrapped_payload, dict):
		return wrapped_payload
	return dict(frappe.form_dict)


@frappe.whitelist(methods=["GET", "POST"])
def get_crm_sales_order_state(crm_order_no=None, crm_order_id=None, erp_order_id=None):
	from crm_integration.crm_integration.order_revision import get_order_state

	return get_order_state(crm_order_no, crm_order_id, erp_order_id)


@frappe.whitelist(methods=["POST"])
def create_crm_sales_order(request=None):
	from crm_integration.crm_integration.order_revision import create_order

	return create_order(request)


@frappe.whitelist(methods=["POST"])
def revise_crm_sales_order(request=None):
	from crm_integration.crm_integration.order_revision import revise_order

	return revise_order(request)


@frappe.whitelist(methods=["POST"])
def revoke_crm_sales_order(request=None):
	from crm_integration.crm_integration.order_revision import revoke_order

	return revoke_order(request)


@frappe.whitelist(methods=["POST"])
def resubmit_crm_sales_order(request=None):
	from crm_integration.crm_integration.order_revision import resubmit_order

	return resubmit_order(request)


@frappe.whitelist(methods=["GET", "POST"])
def get_crm_order_operation_result(operation_id=None, crm_order_no=None):
	from crm_integration.crm_integration.order_revision import get_operation_result

	return get_operation_result(operation_id, crm_order_no)


# ---------------------------------------------------------------------------
# Item Sync APIs
# ---------------------------------------------------------------------------


def _validate_item_group(item_group):
	"""验证物料组存在且为叶子节点"""
	if not item_group:
		frappe.throw(_("缺少必填字段：item_group"))

	if not frappe.db.exists("Item Group", item_group):
		frappe.throw(_("物料组不存在：{0}").format(item_group))

	is_group = frappe.db.get_value("Item Group", item_group, "is_group")
	if is_group:
		frappe.throw(_("物料组 {0} 是父级分组，请选择叶子分组。").format(item_group))


def _validate_uom(uom):
	"""验证单位存在"""
	if not uom:
		frappe.throw(_("缺少必填字段：stock_uom"))

	if not frappe.db.exists("UOM", uom):
		frappe.throw(_("单位不存在：{0}").format(uom))


@frappe.whitelist()
def get_item_group_list(keyword=None):
	"""查询物料组列表，支持模糊搜索。返回叶子物料组。"""
	validate_crm_api_user()
	filters = {"is_group": 0}
	if keyword:
		filters["name"] = ["like", "%{}%".format(keyword)]

	groups = frappe.get_all(
		"Item Group",
		filters=filters,
		fields=["name", "parent_item_group", "item_group_name"],
		order_by="name asc",
		limit_page_length=100,
	)
	return {"status": "success", "data": groups}


@frappe.whitelist()
def check_item_exists(item_code):
	"""检查物料是否已存在，返回完整物料信息"""
	validate_crm_api_user()
	if not item_code:
		frappe.throw(_("缺少必填字段：item_code"))

	exists = frappe.db.exists("Item", item_code)
	if exists:
		fields = [
			# 基础字段
			"item_code", "item_name", "item_group", "stock_uom",
			"description", "brand", "disabled",
			# 自定义字段
			"custom_specifications", "custom_short_name", "custom_item_short_name",
			"custom_mnemonic_code", "custom_sku", "custom_external_code",
			"custom_item_classification", "custom_dpci",
		]
		# 过滤掉不存在的字段
		meta = frappe.get_meta("Item")
		valid_fields = [f for f in fields if meta.has_field(f) or f in frappe.model.default_fields]

		item = frappe.get_value("Item", item_code, valid_fields, as_dict=True)
		return {"status": "success", "exists": True, **item}

	return {"status": "success", "exists": False, "item_code": item_code}


@frappe.whitelist(methods=["POST"])
def create_item_from_crm(item_data=None):
	"""从 CRM 创建物料到 ERP。"""
	validate_crm_api_user()
	payload = get_request_payload(item_data)
	validate_item_payload(payload)

	item_code = payload["item_code"]

	# 检查物料是否已存在
	if frappe.db.exists("Item", item_code):
		item = frappe.get_value(
			"Item", item_code,
			["item_code", "item_name", "item_group"],
			as_dict=True,
		)
		return {"status": "exists", "message": _("物料已存在。"), **item}

	# 校验物料组和单位
	_validate_item_group(payload["item_group"])
	_validate_uom(payload["stock_uom"])

	# 记录日志
	crm_log = create_crm_log(
		direction="Inbound",
		event="Item Create From CRM",
		status="Pending",
		source="CRM",
		request_url=get_request_url(),
		request_payload=payload,
	)

	try:
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": payload["item_name"],
			"item_group": payload["item_group"],
			"stock_uom": payload["stock_uom"],
			"custom_specifications": payload.get("custom_specifications"),
		})
		doc.insert(ignore_permissions=True)

		response = {
			"status": "success",
			"message": _("物料已创建。"),
			"item_code": doc.item_code,
			"item_name": doc.item_name,
			"item_group": doc.item_group,
		}

		update_crm_log(
			crm_log,
			status="Success",
			reference_doctype="Item",
			reference_name=doc.name,
			response_payload=response,
			http_status_code=200,
		)
		return response

	except Exception:
		update_crm_log(
			crm_log,
			status="Failed",
			error_message=frappe.get_traceback(),
			http_status_code=500,
		)
		raise


def validate_item_payload(payload):
	"""校验物料创建请求体"""
	if not isinstance(payload, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))

	if not payload.get("item_code"):
		frappe.throw(_("缺少必填字段：item_code"))

	if not payload.get("item_name"):
		frappe.throw(_("缺少必填字段：item_name"))

	if not payload.get("item_group"):
		frappe.throw(_("缺少必填字段：item_group"))

	if not payload.get("stock_uom"):
		frappe.throw(_("缺少必填字段：stock_uom"))
