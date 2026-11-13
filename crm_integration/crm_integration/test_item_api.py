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
		self.session = self.stack.enter_context(
			patch.object(frappe, "session", frappe._dict(user="ordinary@example.test"))
		)
		self.crm_auth = self.stack.enter_context(patch.object(api, "validate_crm_api_user"))
		self.stack.enter_context(patch.object(api, "_validate_item_group"))
		self.stack.enter_context(patch.object(api, "_validate_uom"))
		self.exists = self.stack.enter_context(patch.object(frappe.db, "exists", return_value=False))
		item_fields = [
			frappe._dict(fieldname=name, fieldtype="Data")
			for name in (*self.payload, *self.custom_fields, "brand", "description")
		]
		item_fields.extend([
			frappe._dict(fieldname="disabled", fieldtype="Check"),
			frappe._dict(fieldname="valuation_rate", fieldtype="Currency"),
			frappe._dict(fieldname="custom_numeric_attribute", fieldtype="Float"),
			frappe._dict(fieldname="uoms", fieldtype="Table", options="UOM Conversion Detail"),
		])
		self.metas = {
			"Item": frappe._dict(fields=item_fields),
			"UOM Conversion Detail": frappe._dict(fields=[
				frappe._dict(fieldname="uom", fieldtype="Link"),
				frappe._dict(fieldname="conversion_factor", fieldtype="Float"),
			]),
		}
		for meta in self.metas.values():
			meta.get_field = lambda fieldname, meta=meta: next(
				(field for field in meta.fields if field.fieldname == fieldname), None
			)
		self.meta = self.stack.enter_context(patch.object(frappe, "get_meta", side_effect=self.metas.get))
		self.has_column = self.stack.enter_context(patch.object(frappe.db, "has_column", return_value=True))
		self.doc = MagicMock()
		self.doc.item_code = self.doc.name = self.payload["item_code"]
		self.doc.item_name = self.payload["item_name"]
		self.doc.item_group = self.payload["item_group"]
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", return_value=self.doc))
		self.log = self.stack.enter_context(patch.object(api, "create_crm_log"))
		self.update_log = self.stack.enter_context(patch.object(api, "update_crm_log"))
		self.stack.enter_context(patch.object(api, "get_request_url", return_value="/api/method/test"))

	def test_standard_and_custom_fields_are_written_without_a_fixed_field_list(self):
		payload = {
			**self.payload, **self.custom_fields,
			"brand": "Test Brand", "description": "Extended description",
			"disabled": 1, "valuation_rate": 100, "custom_numeric_attribute": 2.5,
		}
		self.assertEqual(api.create_item_from_crm(payload)["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item", **payload})
		self.doc.insert.assert_called_once_with(ignore_permissions=True)

	def test_legacy_request_does_not_require_custom_fields(self):
		self.assertEqual(api.create_item_from_crm(self.payload)["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload})

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

	def test_authenticated_non_crm_user_can_create_without_item_permissions(self):
		with patch.object(frappe, "has_permission", return_value=False):
			self.assertEqual(api.create_item_from_crm(self.payload)["status"], "success")
		self.crm_auth.assert_not_called()
		self.doc.insert.assert_called_once_with(ignore_permissions=True)

	def test_guests_and_missing_identity_are_rejected_before_reading_items(self):
		for user in ("Guest", "", None):
			self.session.user = user
			with self.subTest(user=user), self.assertRaises(frappe.AuthenticationError):
				api.create_item_from_crm(self.payload)
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_new_site_custom_field_is_accepted_without_api_changes(self):
		self.metas["Item"].fields.append(frappe._dict(fieldname="custom_new_attribute", fieldtype="Check"))
		payload = {**self.payload, "custom_new_attribute": 1}
		api.create_item_from_crm(payload)
		self.get_doc.assert_called_once_with({"doctype": "Item", **payload})

	def test_bare_json_body_preserves_business_fields(self):
		payload = {**self.payload, "description": "JSON body"}
		request = MagicMock(is_json=True)
		request.get_json.return_value = payload
		with patch.object(frappe, "request", request):
			api.create_item_from_crm()
		self.get_doc.assert_called_once_with({"doctype": "Item", **payload})

	def test_framework_control_and_audit_fields_are_not_copied(self):
		payload = {
			**self.payload,
			"doctype": "User", "name": "Other document", "docstatus": 2,
			"owner": "Administrator", "creation": "2000-01-01 00:00:00",
			"modified_by": "Administrator", "flags": {"ignore_mandatory": True},
			"cmd": "crm_integration.crm_integration.api.create_item_from_crm", "_lang": "zh",
			"__islocal": False,
		}
		api.create_item_from_crm(payload)
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload})

	def test_child_tables_use_their_model_and_do_not_accept_framework_attributes(self):
		row = {
			"uom": "Box", "conversion_factor": 10,
			"doctype": "User", "name": "Other row", "docstatus": 2,
			"owner": "Administrator", "parent": "Other item", "flags": {"ignore_links": True},
		}
		api.create_item_from_crm({**self.payload, "uoms": [row]})
		self.get_doc.assert_called_once_with({
			"doctype": "Item", **self.payload,
			"uoms": [{"doctype": "UOM Conversion Detail", "uom": "Box", "conversion_factor": 10}],
		})
		self.assertEqual(row["doctype"], "User")

	def test_malformed_child_tables_are_rejected_before_insert(self):
		for value in ({"uom": "Box"}, "Box", ["Box"], [None]):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api.create_item_from_crm({**self.payload, "uoms": value})
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_required_fields_are_still_checked(self):
		for field in self.payload:
			with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
				api.create_item_from_crm({**self.payload, field: ""})
		self.get_doc.assert_not_called()

	def test_invalid_item_codes_are_rejected_before_any_item_query(self):
		for value in ({"name": ["like", "%"]}, ["like", "%"], 123, True, " ", "\t\n"):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api.create_item_from_crm({**self.payload, "item_code": value})
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_missing_custom_field_metadata_is_rejected_even_for_empty_values(self):
		self.metas["Item"].fields = [
			field for field in self.metas["Item"].fields if field.fieldname != "custom_dpci"
		]
		for value in ("DPCI-001", "", None):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError) as error:
				api.create_item_from_crm({**self.payload, "custom_dpci": value})
			self.assertIn("custom_dpci", str(error.exception))
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_missing_database_column_is_rejected_before_logging_or_insert(self):
		self.has_column.side_effect = lambda doctype, fieldname: fieldname != "custom_dpci"
		with self.assertRaises(frappe.ValidationError) as error:
			api.create_item_from_crm({**self.payload, "custom_dpci": "DPCI-001"})
		self.assertIn("custom_dpci", str(error.exception))
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_unknown_business_and_method_fields_are_rejected(self):
		for fieldname in ("descripton", "custom_unknown_field", "validate", "insert"):
			with self.subTest(fieldname=fieldname), self.assertRaises(frappe.ValidationError) as error:
				api.create_item_from_crm({**self.payload, fieldname: "unexpected"})
			self.assertIn(fieldname, str(error.exception))
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_child_table_fields_are_checked_against_child_metadata_and_columns(self):
		for fieldname in ("conversion_fator", "conversion_factor"):
			self.has_column.side_effect = lambda doctype, name: name != "conversion_factor"
			with self.subTest(fieldname=fieldname), self.assertRaises(frappe.ValidationError) as error:
				api.create_item_from_crm({**self.payload, "uoms": [{"uom": "Box", fieldname: 10}]})
			self.assertIn(fieldname, str(error.exception))
			self.assertIn("UOM Conversion Detail", str(error.exception))
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_non_json_form_transport_parameters_are_not_treated_as_business_fields(self):
		form_data = frappe._dict({**self.payload, "cmd": "test", "_lang": "zh"})
		with patch.object(frappe, "request", None), patch.object(frappe, "form_dict", form_data):
			self.assertEqual(api.create_item_from_crm()["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item", **self.payload})

	def test_existing_item_replay_does_not_require_new_business_fields_to_be_available(self):
		self.exists.return_value = True
		existing = {**self.payload}
		with patch.object(frappe, "get_value", return_value=existing):
			self.assertEqual(api.create_item_from_crm({**self.payload, "custom_unknown_field": "x"})["status"], "exists")
		self.meta.assert_not_called()
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_native_validation_errors_are_propagated(self):
		self.doc.insert.side_effect = frappe.ValidationError("Native Item validation failed")
		with self.assertRaises(frappe.ValidationError):
			api.create_item_from_crm(self.payload)
		self.assertEqual(self.update_log.call_args.kwargs["status"], "Failed")

	def test_only_post_is_registered(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.create_item_from_crm], ["POST"])
