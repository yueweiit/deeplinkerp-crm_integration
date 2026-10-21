from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from crm_integration.crm_integration import integration_log


class TestCRMIntegrationLog(TestCase):
	def test_create_defaults_missing_http_status_to_zero(self):
		for values in ({}, {"http_status_code": None}):
			with (
				self.subTest(values=values),
				patch.object(frappe, "db", MagicMock()),
				patch.object(frappe, "get_doc") as get_doc,
				patch.object(frappe, "log_error") as log_error,
			):
				result = integration_log.create_crm_log(
					direction="Inbound", event="CRM Shipment Create And Submit", user="Administrator", **values
				)
				self.assertIs(result, get_doc.return_value)
				self.assertEqual(get_doc.call_args.args[0]["http_status_code"], 0)
				get_doc.return_value.insert.assert_called_once_with(ignore_permissions=True)
				log_error.assert_not_called()

	def test_create_preserves_known_http_status(self):
		for code in (0, 200, 409, 500):
			with self.subTest(code=code), patch.object(frappe, "db", MagicMock()), patch.object(frappe, "get_doc") as get_doc:
				integration_log.create_crm_log(
					direction="Inbound", event="CRM Shipment Create And Submit", user="Administrator",
					http_status_code=code,
				)
				self.assertEqual(get_doc.call_args.args[0]["http_status_code"], code)

	def test_failure_update_without_http_response_preserves_original_error(self):
		log = frappe._dict(name="CRM-LOG-TEST")
		for error_message in (
			"Unknown column 'custom_crm_repost_ids' in 'SET'",
			"frappe.exceptions.ValidationError: 新事件执行必须提供 source_fingerprint 和 preview_fingerprint，请先预览。",
		):
			with (
				self.subTest(error_message=error_message),
				patch.object(frappe, "db", MagicMock()) as db,
				patch.object(frappe, "log_error") as log_error,
			):
				integration_log.update_crm_log(
					log, status="Failed", http_status_code=None, error_message=error_message,
				)
				db.set_value.assert_called_once_with(
					"CRM Integration Log", log.name,
					{"status": "Failed", "http_status_code": 0, "error_message": error_message},
					update_modified=True,
				)
				log_error.assert_not_called()

	def test_update_preserves_known_http_status(self):
		for code in (0, 200, 409, 500):
			with self.subTest(code=code), patch.object(frappe, "db", MagicMock()) as db:
				integration_log.update_crm_log(frappe._dict(name="CRM-LOG-TEST"), http_status_code=code)
				self.assertEqual(db.set_value.call_args.args[2]["http_status_code"], code)

	def test_update_does_not_overwrite_omitted_http_status(self):
		with patch.object(frappe, "db", MagicMock()) as db:
			integration_log.update_crm_log(frappe._dict(name="CRM-LOG-TEST"), status="Failed")
			self.assertEqual(db.set_value.call_args.args[2], {"status": "Failed"})

	def test_update_serializes_payload_and_preserves_other_null_fields(self):
		with patch.object(frappe, "db", MagicMock()) as db:
			integration_log.update_crm_log(
				frappe._dict(name="CRM-LOG-TEST"), response_payload={"ok": False}, error_message=None,
			)
			values = db.set_value.call_args.args[2]
			self.assertEqual(frappe.parse_json(values["response_payload"]), {"ok": False})
			self.assertIsNone(values["error_message"])

	def test_update_without_log_does_not_write(self):
		with patch.object(frappe, "db", MagicMock()) as db:
			integration_log.update_crm_log(None, http_status_code=None)
			db.set_value.assert_not_called()
