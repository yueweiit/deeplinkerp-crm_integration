import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields(
		{
			"Sales Order Item": [
				{
					"fieldname": "custom_mes_route_scenario",
					"fieldtype": "Data",
					"label": "MES Route Scenario",
					"length": 64,
					"insert_after": "custom_version",
					"allow_on_submit": 1,
				},
			],
		},
		update=True,
	)
	frappe.clear_cache(doctype="Sales Order Item")
