"""Order-level production routing shared by financial release and MES callbacks."""

import json
import re

import frappe
from frappe import _
from frappe.utils import flt


DIRECT_PRODUCT_FIELD = "custom_direct_fulfillment_product_prefixes"
ORDER_ROUTE_FIELD = "custom_crm_fulfillment_route"
DEFAULT_DIRECT_PRODUCT_PREFIXES = frozenset({"NSF"})
DIRECT = "DIRECT"
MES = "MES"
MIXED = "MIXED"
UNKNOWN = "UNKNOWN"


def parse_direct_product_prefixes(value):
	if value is None:
		return frozenset()
	if not isinstance(value, str):
		frappe.throw(_("Enter one three-character alphanumeric product prefix per line (for example, NSF). Wildcards are not allowed."))
	prefixes = {part.upper() for part in re.split(r"[\s,，;；]+", value.strip()) if part}
	if any(not re.fullmatch(r"[A-Z0-9]{3}", prefix) for prefix in prefixes):
		frappe.throw(_("Enter one three-character alphanumeric product prefix per line (for example, NSF). Wildcards are not allowed."))
	return frozenset(prefixes)


def get_company_direct_product_prefixes(company):
	# Keep the original NSF rule until the new Company field has been synchronized.
	if not isinstance(company, str) or not company or not frappe.db.has_column("Company", DIRECT_PRODUCT_FIELD):
		return DEFAULT_DIRECT_PRODUCT_PREFIXES
	return parse_direct_product_prefixes(frappe.db.get_value("Company", company, DIRECT_PRODUCT_FIELD))


def validate_company_direct_products(doc, method=None):
	if not doc.meta.has_field(DIRECT_PRODUCT_FIELD):
		return
	previous = doc.get_doc_before_save()
	old_value = previous.get(DIRECT_PRODUCT_FIELD) if previous else None
	prefixes = parse_direct_product_prefixes(doc.get(DIRECT_PRODUCT_FIELD))
	old_prefixes = parse_direct_product_prefixes(old_value) if previous else DEFAULT_DIRECT_PRODUCT_PREFIXES
	if prefixes != old_prefixes:
		if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
			frappe.throw(_("Only System Managers can change direct-fulfillment product rules."), frappe.PermissionError)
	doc.set(DIRECT_PRODUCT_FIELD, "\n".join(sorted(prefixes)))


def get_production_route(sales_order):
	rows = sales_order.get("items") or []
	# Missing or invalid lines must never qualify an order for the MES exemption.
	if not isinstance(rows, (list, tuple)) or not rows:
		return MES
	prefixes = get_company_direct_product_prefixes(sales_order.get("company"))
	direct = []
	products = set()
	for row in rows:
		code = row.get("item_code")
		product = code.strip().upper()[:3] if isinstance(code, str) else ""
		products.add(product)
		direct.append(
			bool(product) and product in prefixes
			and flt(row.get("qty")) > 0
		)
	if all(direct) and len(products) == 1:
		return DIRECT
	return MIXED if any(direct) else MES


def validate_production_route(sales_order):
	route = get_effective_production_route(sales_order)
	if route == MIXED:
		frappe.throw(_("直接履约产品与其他产品不能在同一订单放行，请按产品拆分销售订单后重试。"))
	if route == UNKNOWN:
		frappe.throw(_("Historical order fulfillment route is unknown. Verify its original MES handling before retrying."))
	return route


def route_from_audit(log):
	if not isinstance(log, dict):
		return None
	for field in ("response_payload", "request_payload"):
		value = log.get(field)
		if isinstance(value, str):
			try:
				value = json.loads(value)
			except ValueError:
				continue
		if isinstance(value, dict) and isinstance(value.get("fulfillment_route"), str) and value["fulfillment_route"] in {DIRECT, MES}:
			return value["fulfillment_route"]
	return None


def legacy_state_route(sales_order):
	# Before direct fulfillment, Pending Production was reached only after MES accepted B1.
	if sales_order.get("custom_process_status") == "Pending Production":
		return MES
	if sales_order.get("docstatus") == 2 or (
		sales_order.get("docstatus") == 1 and sales_order.get("custom_process_status") not in {
			"Pending Confirmation", "Pending Deposit Confirmation", "Deposit Confirmation Processing",
		}
	):
		return UNKNOWN
	return None


def get_recorded_production_route(sales_order):
	"""Keep a completed order's route stable when Company product rules change later."""
	stored = sales_order.get(ORDER_ROUTE_FIELD)
	if stored in (DIRECT, MES, UNKNOWN):
		return stored
	name = sales_order.get("name")
	if not isinstance(name, str) or not name or not sales_order.get("custom_crm_order_no"):
		return None
	if frappe.db.exists("DocType", "CRM Integration Log"):
		log = frappe.db.get_value("CRM Integration Log", {
			"event": "Finance Production Release", "reference_doctype": "Sales Order",
			"reference_name": name, "status": "Success",
		}, ["response_payload", "request_payload"], order_by="creation desc", as_dict=True)
		if route := route_from_audit(log):
			return route
	# Older manufacturing orders may predate financial-route snapshots.
	if frappe.db.exists("DocType", "MES Integration Log") and frappe.db.exists("MES Integration Log", {
		"event": "Sales Order Push To MES", "reference_doctype": "Sales Order",
		"reference_name": name, "status": "Success",
	}):
		return MES
	return legacy_state_route(sales_order)


def get_effective_production_route(sales_order):
	return get_recorded_production_route(sales_order) or get_production_route(sales_order)


def is_direct_fulfillment_order(sales_order):
	route = get_effective_production_route(sales_order)
	if route == UNKNOWN:
		frappe.throw(_("Historical order fulfillment route is unknown. Verify its original MES handling before retrying."))
	return route == DIRECT


def assert_order_route_schema():
	if not frappe.db.has_column("Sales Order", ORDER_ROUTE_FIELD):
		frappe.throw(_("Order fulfillment route schema is outdated. Please migrate the site before financial release."))


def record_completed_route(sales_order, route):
	assert_order_route_schema()
	if route not in (DIRECT, MES) or sales_order.get(ORDER_ROUTE_FIELD) not in (None, "", route):
		frappe.throw(_("Stored order fulfillment route conflicts with the completed release."))
	sales_order.db_set(ORDER_ROUTE_FIELD, route, update_modified=False)


def protect_recorded_production_route(doc, method=None):
	previous = doc.get_doc_before_save()
	old = previous.get(ORDER_ROUTE_FIELD) if previous else None
	if (doc.get(ORDER_ROUTE_FIELD) or "") != (old or ""):
		frappe.throw(_("Order fulfillment route can only be recorded by the financial release workflow."), frappe.PermissionError)
