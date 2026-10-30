"""Add the administrator-maintained, Company-scoped direct-product list."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from crm_integration.crm_integration.production_routing import (
	DIRECT_PRODUCT_FIELD, ORDER_ROUTE_FIELD, MES, legacy_state_route, route_from_audit,
)


def execute():
	new_column = not frappe.db.has_column("Company", DIRECT_PRODUCT_FIELD)
	create_custom_fields({"Company": [{
		"fieldname": DIRECT_PRODUCT_FIELD,
		"fieldtype": "Small Text",
		"insert_after": "custom_enable_crm_integration",
		"label": "Direct Fulfillment Product Prefixes",
		"description": "One three-character product prefix per line, for example NSF. All models and colors of that product follow the whole order. Empty means no direct products. Only System Managers can change this list.",
		"default": "NSF",
		"depends_on": "eval:doc.custom_enable_crm_integration",
		"read_only_depends_on": "eval:frappe.session.user != 'Administrator' && !frappe.user_roles.includes('System Manager')",
	}], "Sales Order": [{
		"fieldname": ORDER_ROUTE_FIELD,
		"fieldtype": "Select",
		"insert_after": "custom_process_status",
		"label": "CRM Fulfillment Route",
		"options": "\nDIRECT\nMES\nUNKNOWN",
		"read_only": 1,
		"hidden": 1,
		"allow_on_submit": 1,
		"no_copy": 1,
	}]}, update=True)
	if new_column:
		company = frappe.qb.DocType("Company")
		column = company[DIRECT_PRODUCT_FIELD]
		frappe.qb.update(company).set(column, "NSF").where(column.isnull() | (column == "")).run()
	backfill_order_routes()
	frappe.clear_cache(doctype="Company")
	frappe.clear_cache(doctype="Sales Order")


def backfill_order_routes(batch_size=200):
	"""Retain available historical evidence before integration-log retention removes it."""
	from crm_integration.crm_integration.finance_release import AUDIT_EVENT, latest_order_logs

	after = ""
	order = frappe.qb.DocType("Sales Order")
	column = order[ORDER_ROUTE_FIELD]
	while True:
		rows = frappe.get_all("Sales Order", filters={
			ORDER_ROUTE_FIELD: ["is", "not set"], "custom_crm_order_no": ["is", "set"], "name": [">", after],
		}, fields=["name", "docstatus", "custom_process_status"], order_by="name asc", limit=batch_size)
		if not rows:
			return
		names = [row.name for row in rows]
		routes = {}
		if frappe.db.exists("DocType", "CRM Integration Log"):
			for log in latest_order_logs("CRM Integration Log", AUDIT_EVENT, names,
				("reference_name", "status", "response_payload", "request_payload"), status="Success"):
				if log.status == "Success" and (route := route_from_audit(log)):
					routes[log.reference_name] = route
		if frappe.db.exists("DocType", "MES Integration Log"):
			for row in frappe.get_all("MES Integration Log", filters={
				"event": "Sales Order Push To MES", "reference_doctype": "Sales Order",
				"reference_name": ["in", names], "status": "Success",
			}, fields=["reference_name"], distinct=True, limit=0):
				routes.setdefault(row.reference_name, MES)
		groups = {}
		for row in rows:
			route = routes.get(row.name) or legacy_state_route(row)
			if route:
				groups.setdefault(route, []).append(row.name)
		for route, members in groups.items():
			# Never replace a route completed concurrently by a worker or a previous migration.
			frappe.qb.update(order).set(column, route).where(
				order.name.isin(members) & (column.isnull() | (column == ""))
			).run()
		after = names[-1]
