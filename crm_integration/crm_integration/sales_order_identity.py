import hashlib
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint


CRM_ORDER_LOCK_TIMEOUT = 10


def get_sales_order_names_by_crm_order_no(crm_order_no, limit=2, *, docstatus=None):
	filters = {"custom_crm_order_no": crm_order_no}
	if docstatus is not None:
		filters["docstatus"] = docstatus
	return frappe.get_all(
		"Sales Order",
		filters=filters,
		pluck="name",
		limit_page_length=limit,
	)


def make_crm_order_lock_name(crm_order_no):
	digest = hashlib.sha224(str(crm_order_no).encode()).hexdigest()
	return f"crm-so-{digest}"


@contextmanager
def crm_sales_order_creation_lock(crm_order_no):
	"""Serialize Sales Order creation for one CRM order number across workers."""
	lock_name = make_crm_order_lock_name(crm_order_no)
	timeout = cint(frappe.conf.get("crm_order_lock_timeout") or CRM_ORDER_LOCK_TIMEOUT)
	result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, timeout))
	acquired = bool(result and cint(result[0][0]) == 1)
	if not acquired:
		frappe.throw(_("CRM 销售订单号 {0} 正在处理中，请稍后重试。").format(crm_order_no))

	try:
		yield
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
