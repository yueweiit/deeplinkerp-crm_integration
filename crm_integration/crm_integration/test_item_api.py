from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import api


class TestCRMItemAPI(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.payload = {
			"item_code": "CRM-TEST-ITEM",
			"item_name": "CRM Test Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}
		self.custom_fields = {
			"custom_dpci": "DPCI-001",
			"custom_external_code": "CRM-001",
			"custom_short_name": "Upper short name",
			"custom_sku": "SKU-001",
			"custom_specifications": "Black",
			"custom_item_short_name": "Lower short name",
			"custom_mnemonic_code": "TEST",
		}
		self.stack = self.enterContext(ExitStack())
		self.auth = self.stack.enter_context(patch.object(api, "validate_crm_api_user"))
		self.stack.enter_context(patch.object(api, "_validate_item_group"))
		self.stack.enter_context(patch.object(api, "_validate_uom"))
		self.exists = self.stack.enter_context(patch.object(frappe.db, "exists", return_value=False))
		self.meta = self.stack.enter_context(patch.object(frappe, "get_meta"))
		self.meta.return_value.has_field.return_value = True
		self.has_column = self.stack.enter_context(patch.object(frappe.db, "has_column", return_value=True))
		self.doc = MagicMock()
		self.doc.item_code = self.doc.name = self.payload["item_code"]
		self.doc.item_name = self.payload["item_name"]
		self.doc.item_group = self.payload["item_group"]
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", return_value=self.doc))
		self.log = self.stack.enter_context(patch.object(api, "create_crm_log"))
		self.stack.enter_context(patch.object(api, "update_crm_log"))
		self.stack.enter_context(patch.object(api, "get_request_url", return_value="/api/method/test"))

	def test_custom_fields_are_written_independently_and_extra_fields_are_ignored(self):
		payload = {**self.payload, **self.custom_fields, "disabled": 1, "valuation_rate": 100}
		result = api.create_item_from_crm(payload)
		self.auth.assert_called_once()
		self.assertEqual(result["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload, **self.custom_fields})
		self.doc.insert.assert_called_once_with(ignore_permissions=True)

	def test_legacy_request_does_not_require_custom_fields(self):
		self.assertEqual(api.create_item_from_crm(self.payload)["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload})
		self.meta.assert_not_called()

	def test_json_string_preserves_custom_fields(self):
		api.create_item_from_crm(frappe.as_json({**self.payload, **self.custom_fields}))
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload, **self.custom_fields})

	def test_empty_and_null_custom_fields_are_allowed(self):
		fields = {"custom_short_name": "", "custom_item_short_name": None}
		api.create_item_from_crm({**self.payload, **fields})
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload, **fields})

	def test_existing_item_is_not_updated(self):
		self.exists.return_value = True
		existing = {"item_code": self.payload["item_code"], "item_name": "Original", "item_group": "Products"}
		with patch.object(frappe, "get_value", return_value=existing):
			self.assertEqual(api.create_item_from_crm({**self.payload, **self.custom_fields})["status"], "exists")
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_authentication_is_required_before_creating_item(self):
		self.auth.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			api.create_item_from_crm({**self.payload, **self.custom_fields})
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_non_text_custom_fields_are_rejected(self):
		for field in self.custom_fields:
			for value in (123, False, [], {}):
				with self.subTest(field=field, value=value), self.assertRaises(frappe.ValidationError):
					api.create_item_from_crm({**self.payload, field: value})
		self.get_doc.assert_not_called()

	def test_missing_metadata_or_column_is_rejected_before_logging_or_insert(self):
		for has_field, has_column in ((False, True), (True, False)):
			self.meta.return_value.has_field.return_value = has_field
			self.has_column.return_value = has_column
			with self.subTest(has_field=has_field, has_column=has_column), self.assertRaises(frappe.ValidationError):
				api.create_item_from_crm({**self.payload, "custom_dpci": "DPCI-001"})
		self.get_doc.assert_not_called()
		self.log.assert_not_called()
