"""Optional CRM order snapshot fields for product origin."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	field = {
		"fieldname": "custom_product_source",
		"fieldtype": "Select",
		"label": "Product Source",
		"options": "\nSELF_DEVELOPED\nEXTERNAL_PURCHASE",
		"read_only": 1,
		"no_copy": 1,
	}
	create_custom_fields({
		"Sales Order": [{**field, "insert_after": "custom_crm_order_no"}],
		"Sales Order Item": [{**field, "insert_after": "custom_product"}],
	}, update=True)
	frappe.clear_cache(doctype="Sales Order")
	frappe.clear_cache(doctype="Sales Order Item")
