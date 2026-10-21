import frappe

from crm_integration.crm_integration.auth import is_crm_api_user
from crm_integration.crm_integration.settings import is_crm_integration_enabled


def create_crm_log(
	direction,
	event,
	status="Success",
	reference_doctype=None,
	reference_name=None,
	source=None,
	user=None,
	request_url=None,
	request_payload=None,
	response_payload=None,
	error_message=None,
	trace_id=None,
	external_status=None,
	http_status_code=None,
):
	if not frappe.db.exists("DocType", "CRM Integration Log"):
		return None

	try:
		log = frappe.get_doc(
			{
				"doctype": "CRM Integration Log",
				"direction": direction,
				"event": event,
				"status": status,
				"source": source,
				"user": user or get_current_user(),
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"request_url": request_url,
				"request_payload": as_json(request_payload),
				"response_payload": as_json(response_payload),
				"error_message": error_message,
				"trace_id": trace_id,
				"external_status": external_status,
				"http_status_code": http_status_code if http_status_code is not None else 0,
			}
		)
		log.insert(ignore_permissions=True)
		return log
	except Exception:
		frappe.log_error(title="Failed to create CRM Integration Log", message=frappe.get_traceback())
		return None


def update_crm_log(log, **values):
	if not log:
		return

	try:
		updates = {}
		for fieldname, value in values.items():
			if fieldname in ("request_payload", "response_payload"):
				updates[fieldname] = as_json(value)
			elif fieldname == "http_status_code" and value is None:
				updates[fieldname] = 0
			else:
				updates[fieldname] = value

		frappe.db.set_value("CRM Integration Log", log.name, updates, update_modified=True)
	except Exception:
		frappe.log_error(title="Failed to update CRM Integration Log", message=frappe.get_traceback())


def get_current_user():
	return getattr(frappe.session, "user", None)


def as_json(value):
	if value is None or isinstance(value, str):
		return value

	return frappe.as_json(value, indent=2)


def log_inbound_sales_order(doc, method=None):
	if not is_crm_integration_enabled(doc.get("company")):
		return

	create_crm_log(
		direction="Inbound",
		event="Sales Order Created",
		status="Success",
		reference_doctype="Sales Order",
		reference_name=doc.name,
		source=get_request_source(),
		request_url=get_request_url(),
		request_payload=doc.as_dict(no_nulls=True),
		response_payload={"docstatus": doc.docstatus},
	)


def get_request_source():
	if not getattr(frappe.local, "request", None):
		return "Background"

	path = getattr(frappe.request, "path", "") or ""
	if path.startswith("/api/"):
		if is_crm_api_user():
			return "CRM"
		return "External API"

	return "Desk"


def get_request_url():
	if not getattr(frappe.local, "request", None):
		return None

	return getattr(frappe.request, "url", None)
