import frappe
from frappe.utils import cint


def is_crm_integration_enabled(company):
	if not company or not frappe.db.has_column("Company", "custom_enable_crm_integration"):
		return False

	return bool(frappe.db.get_value("Company", company, "custom_enable_crm_integration"))


def is_crm_shipment_status_push_enabled():
	return cint(frappe.conf.get("crm_shipment_status_push_enabled", 1)) == 1


def throw_crm_integration_disabled(company):
	frappe.throw(frappe._("公司 {0} 未启用 CRM 集成。").format(company or ""))
