import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

PROCESS_STATUS_OPTIONS = "\n".join(
	[
		"Pending Confirmation",
		"Pending Deposit Confirmation",
		"Deposit Confirmation Processing",
		"Pending Production",
		"Pending Final Payment",
		"Deliverable",
		"Partially Delivered",
		"Completed",
		"Rejected",
		"Cancelled",
	]
)
CRM_TEMPORARY_RECEIPT_TYPE = "CRM Outbound Temporary Receipt"


def execute():
	create_crm_custom_fields()
	ensure_crm_temporary_receipt_type()
	clear_crm_custom_field_cache()


def ensure_crm_temporary_receipt_type():
	"""A native Material Receipt purpose with explicit provenance and MES-safe classification."""
	if frappe.db.exists("Stock Entry Type", CRM_TEMPORARY_RECEIPT_TYPE):
		if frappe.db.get_value("Stock Entry Type", CRM_TEMPORARY_RECEIPT_TYPE, "purpose") != "Material Receipt":
			frappe.throw("CRM Outbound Temporary Receipt 的 purpose 必须是 Material Receipt；不能覆盖现有类型。")
		return
	frappe.get_doc(dict(doctype="Stock Entry Type", name=CRM_TEMPORARY_RECEIPT_TYPE,
		purpose="Material Receipt", is_standard=0)).insert(ignore_permissions=True)


def create_crm_custom_fields():
	create_custom_fields(
		{
			"Item": [
				{
					"fieldname": "custom_specifications",
					"fieldtype": "Data",
					"label": "Specifications",
					"insert_after": "item_name",
				},
			],
			"Sales Order": [
				{
					"fieldname": "custom_process_status",
					"fieldtype": "Select",
					"label": "Process Status",
					"options": PROCESS_STATUS_OPTIONS,
					"default": "Pending Confirmation",
					"insert_after": "status",
					"in_list_view": 1,
					"allow_on_submit": 1,
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_order_no",
					"fieldtype": "Data",
					"label": "CRM Order No",
					"insert_after": "po_no",
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_order_id",
					"fieldtype": "Data",
					"label": "CRM Order ID",
					"insert_after": "custom_crm_order_no",
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_approval_round",
					"fieldtype": "Int",
					"label": "CRM Approval Round",
					"insert_after": "custom_crm_order_id",
					"read_only": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_revision",
					"fieldtype": "Int",
					"label": "CRM Revision",
					"insert_after": "custom_crm_approval_round",
					"read_only": 1,
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_production_started",
					"fieldtype": "Check",
					"label": "CRM Production Started",
					"insert_after": "custom_crm_revision",
					"read_only": 1,
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_crm_production_milestone",
					"fieldtype": "Select",
					"label": "CRM Production Milestone",
					"options": "\nNOT_STARTED\nATTEMPTED\nREACHED",
					"insert_after": "custom_crm_production_started",
					"read_only": 1,
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_odt",
					"fieldtype": "Data",
					"label": "ODT",
					"insert_after": "custom_crm_production_milestone",
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				{
					"fieldname": "custom_remark",
					"fieldtype": "Small Text",
					"label": "Remark",
					"insert_after": "custom_odt",
					"allow_on_submit": 1,
				},
			],
			"Delivery Note": [
				{
					"fieldname": "custom_crm_shipment_no",
					"fieldtype": "Data",
					"label": "CRM Shipment No",
					"length": 140,
					"insert_after": "customer",
					"read_only": 1,
					"in_list_view": 1,
					"in_standard_filter": 1,
					"unique": 1,
					"allow_on_submit": 1,
					"no_copy": 1,
				},
				*audit_fields([
					("custom_crm_source_organization", "CRM Source Organization", "Data", None),
					("custom_crm_event_id", "CRM Event ID", "Data", None),
					("custom_crm_batch_no", "CRM Human Batch No", "Data", None),
					("custom_crm_historical_replay", "CRM Historical Replay", "Check", None),
					("custom_crm_source_fingerprint", "CRM Source Fingerprint", "Data", None),
					("custom_crm_stock_supplement", "CRM Stock Supplement", "Link", "Stock Entry"),
					("custom_crm_valuation_policy", "CRM Temporary Valuation Policy", "Data", None),
					("custom_crm_repost_ids", "CRM Native Repost IDs", "Small Text", None),
				], "custom_crm_shipment_no"),
			],
			"Sales Order Item": [
				*audit_fields([("custom_crm_order_item_id", "CRM Order Item ID", "Data", None)], "item_code"),
				{
					"fieldname": "custom_product",
					"fieldtype": "Data",
					"label": "Product",
					"insert_after": "item_name",
				},
				{
					"fieldname": "custom_specifications",
					"fieldtype": "Data",
					"label": "Specifications",
					"insert_after": "custom_product",
				},
				{
					"fieldname": "custom_version",
					"fieldtype": "Data",
					"label": "Version",
					"length": 64,
					"insert_after": "custom_specifications",
				},
			],
			"Delivery Note Item": [
				*audit_fields([
					("custom_crm_order_item_id", "CRM Order Item ID", "Data", None),
					("custom_crm_source_unit_price", "CRM Effective Source Unit Price", "Float", None),
					("custom_crm_source_currency", "CRM Source Currency", "Link", "Currency"),
					("custom_crm_source_uom", "CRM Original Unit", "Data", None),
				], "item_code"),
				{
					"fieldname": "custom_version",
					"fieldtype": "Data",
					"label": "Version",
					"length": 64,
					"insert_after": "description",
					"read_only": 1,
				},
			],
			"Stock Entry": audit_fields([
				("custom_crm_shipment_no", "CRM Shipment Event Key", "Data", None),
				("custom_crm_delivery_note", "CRM Delivery Note", "Link", "Delivery Note"),
				("custom_crm_source_fingerprint", "CRM Source Fingerprint", "Data", None),
				("custom_crm_valuation_policy", "CRM Temporary Valuation Policy", "Data", None),
			], "remarks"),
			"Stock Entry Detail": audit_fields([
				("custom_crm_order_item_id", "CRM Order Item ID", "Data", None),
				("custom_crm_sales_order_item", "CRM Sales Order Item", "Data", None),
			], "item_code"),
		},
		update=True,
	)


def audit_fields(definitions, insert_after):
	fields = []
	for fieldname, label, fieldtype, options in definitions:
		field = dict(fieldname=fieldname, label=label, fieldtype=fieldtype, read_only=1,
			allow_on_submit=1, no_copy=1, insert_after=insert_after)
		if options:
			field["options"] = options
		if fieldtype == "Data":
			field["length"] = 140
		fields.append(field)
		insert_after = fieldname
	return fields


def clear_crm_custom_field_cache():
	for doctype in (
		"Item",
		"Sales Order",
		"Sales Order Item",
		"Delivery Note",
		"Delivery Note Item",
		"Stock Entry",
		"Stock Entry Detail",
	):
		frappe.clear_cache(doctype=doctype)
