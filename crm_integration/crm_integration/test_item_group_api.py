from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import api


class TestCRMItemGroupAPI(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.stack = self.enterContext(ExitStack())
		self.session = self.stack.enter_context(patch.object(frappe, "session", frappe._dict(user="ordinary@example.test")))
		self.payload = {"item_group_name": "Test Group", "parent_item_group": "Root", "is_group": 1}
		self.doc = MagicMock()
		for fieldname, value in {"name": "Test Group", "lft": 10, "rgt": 15, **self.payload}.items():
			setattr(self.doc, fieldname, value)
		self.doc.update.side_effect = lambda data: [setattr(self.doc, key, value) for key, value in data.items()]
		self.fields = {
			name: frappe._dict(fieldname=name, fieldtype=fieldtype)
			for name, fieldtype in (
				("item_group_name", "Data"), ("parent_item_group", "Link"), ("is_group", "Check"),
				("image", "Attach Image"), ("custom_group_marker", "Data"),
			)
		}
		meta = MagicMock()
		meta.get_field.side_effect = self.fields.get
		self.stack.enter_context(patch.object(frappe, "get_meta", return_value=meta))
		self.stack.enter_context(patch.object(frappe.db, "has_column", return_value=True))
		self.exists = self.stack.enter_context(patch.object(frappe.db, "exists", return_value=False))
		self.parent = frappe._dict(name="Root", is_group=1, lft=1, rgt=50)
		self.get_value = self.stack.enter_context(patch.object(frappe.db, "get_value", return_value=self.parent))
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", side_effect=self.get_document))
		self.log = self.stack.enter_context(patch.object(api, "create_crm_log"))
		self.update_log = self.stack.enter_context(patch.object(api, "update_crm_log"))
		self.stack.enter_context(patch.object(api, "get_request_url", return_value="/api/method/test"))
		self.rename = self.stack.enter_context(patch("frappe.model.rename_doc.rename_doc", return_value="Renamed Group"))
		self.enqueue = self.stack.enter_context(patch.object(frappe, "enqueue"))

	def get_document(self, data, name=None, **kwargs):
		if isinstance(data, dict):
			for fieldname, value in data.items():
				setattr(self.doc, fieldname, value)
			self.doc.name = data["item_group_name"]
		elif name:
			self.doc.name = name
		return self.doc

	def test_authenticated_user_can_create_without_doctype_permissions(self):
		with patch.object(frappe, "has_permission", return_value=False):
			response = api.create_item_group_from_crm(self.payload)
		self.assertEqual(response["status"], "success")
		self.assertEqual(response["name"], "Test Group")
		self.doc.insert.assert_called_once_with(ignore_permissions=True)
		self.update_log.assert_called_once()
		self.assertEqual(self.update_log.call_args.kwargs["reference_doctype"], "Item Group")

	def test_create_defaults_to_actual_root_without_hardcoded_name(self):
		with patch("frappe.utils.nestedset.get_root_of", return_value="Root"):
			api.create_item_group_from_crm({"item_group_name": "Test Group"})
		self.get_doc.assert_called_once_with({"doctype": "Item Group", "item_group_name": "Test Group", "parent_item_group": "Root"})

	def test_existing_group_returns_exists_without_overwriting(self):
		self.exists.return_value = True
		response = api.create_item_group_from_crm({**self.payload, "is_group": 0})
		self.assertEqual(response["status"], "exists")
		self.assertEqual(response["is_group"], 1)
		self.doc.insert.assert_not_called()
		self.log.assert_not_called()

	def test_guest_or_missing_identity_is_rejected_by_both_endpoints(self):
		for user in ("Guest", "", None):
			self.session.user = user
			for create in (True, False):
				with self.subTest(user=user, create=create), self.assertRaises(frappe.AuthenticationError):
					if create:
						api.create_item_group_from_crm(self.payload)
					else:
						api.update_item_group_from_crm("Test Group", {"image": "/files/test.png"})
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_names_and_update_locator_must_be_non_empty_strings(self):
		for value in ({"name": ["like", "%"]}, ["x"], 123, True, " ", None):
			with self.subTest(value=value, create=True), self.assertRaises(frappe.ValidationError):
				api.create_item_group_from_crm({**self.payload, "item_group_name": value})
			with self.subTest(value=value, create=False), self.assertRaises(frappe.ValidationError):
				api.update_item_group_from_crm(value, {"image": "/files/test.png"})
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()

	def test_parent_must_exist_and_be_a_group_node(self):
		for parent in (None, frappe._dict(name="Leaf", is_group=0)):
			self.get_value.return_value = parent
			with self.subTest(parent=parent), self.assertRaises(frappe.ValidationError):
				api.create_item_group_from_crm(self.payload)
		self.doc.insert.assert_not_called()
		self.log.assert_not_called()

	def test_invalid_parent_values_are_rejected(self):
		for value in (None, "", {}, ["Root"], 123):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api.create_item_group_from_crm({**self.payload, "parent_item_group": value})
		self.doc.insert.assert_not_called()

	def test_tree_indices_cannot_be_supplied(self):
		for fieldname in ("lft", "rgt", "old_parent"):
			with self.subTest(fieldname=fieldname), self.assertRaises(frappe.ValidationError):
				api.create_item_group_from_crm({**self.payload, fieldname: 1})
			with self.subTest(fieldname=fieldname, update=True), self.assertRaises(frappe.ValidationError):
				api.update_item_group_from_crm("Test Group", {fieldname: 1})
		self.doc.insert.assert_not_called()
		self.doc.save.assert_not_called()

	def test_group_flag_accepts_only_boolean_or_zero_and_one(self):
		for value in (2, -1, 1.0, "true", [], {}, None):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api.create_item_group_from_crm({**self.payload, "is_group": value})
		self.doc.insert.assert_not_called()

	def test_all_model_business_fields_are_accepted_and_audit_fields_are_not_copied(self):
		payload = {**self.payload, "custom_group_marker": "custom", "owner": "Administrator", "flags": {"ignore_links": True}}
		api.create_item_group_from_crm(payload)
		self.get_doc.assert_called_once_with({"doctype": "Item Group", **self.payload, "custom_group_marker": "custom"})

	def test_unknown_fields_are_rejected_before_insert(self):
		with self.assertRaises(frappe.ValidationError):
			api.create_item_group_from_crm({**self.payload, "custom_missing": "data"})
		self.doc.insert.assert_not_called()
		self.log.assert_not_called()

	def test_update_changes_only_supplied_fields(self):
		response = api.update_item_group_from_crm("Test Group", {"image": "/files/test.png"})
		self.assertEqual(response["name"], "Test Group")
		self.doc.update.assert_called_once_with({"image": "/files/test.png"})
		self.doc.save.assert_called_once_with(ignore_permissions=True)
		self.rename.assert_not_called()

	def test_update_renames_through_native_api_and_rebuilds_search_after_commit(self):
		response = api.update_item_group_from_crm("Test Group", {"item_group_name": "Renamed Group"})
		self.rename.assert_called_once_with(
			"Item Group", "Test Group", "Renamed Group", ignore_permissions=True,
			merge=False, show_alert=False, rebuild_search=False,
		)
		self.enqueue.assert_called_once_with("frappe.utils.global_search.rebuild_for_doctype", doctype="Item Group", enqueue_after_commit=True)
		self.assertEqual(response["name"], "Renamed Group")

	def test_update_rejects_root_and_circular_parent_changes(self):
		self.doc.parent_item_group = ""
		with self.assertRaises(frappe.ValidationError):
			api.update_item_group_from_crm("Test Group", {"image": "/files/test.png"})
		self.doc.parent_item_group = "Root"
		for lft in (10, 12):
			self.parent.lft = lft
			with self.subTest(lft=lft), self.assertRaises(frappe.ValidationError):
				api.update_item_group_from_crm("Test Group", {"parent_item_group": "Descendant"})
		self.doc.save.assert_not_called()
		self.rename.assert_not_called()

	def test_update_rejects_empty_changes(self):
		with self.assertRaises(frappe.ValidationError):
			api.update_item_group_from_crm("Test Group", {})
		self.doc.save.assert_not_called()

	def test_leaf_conversion_uses_locked_bounds_even_when_snapshot_has_no_children(self):
		with patch.object(frappe, "get_all", return_value=[]) as snapshot_query:
			for value in (0, False, "0"):
				with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
					api.update_item_group_from_crm("Test Group", {"is_group": value})
			snapshot_query.assert_not_called()
		self.assertTrue(all(call.kwargs.get("for_update") for call in self.get_doc.call_args_list))
		self.doc.update.assert_not_called()
		self.doc.save.assert_not_called()
		self.log.assert_not_called()
		self.rename.assert_not_called()
		self.enqueue.assert_not_called()

	def test_empty_group_can_be_converted_to_leaf(self):
		self.doc.rgt = self.doc.lft + 1
		response = api.update_item_group_from_crm("Test Group", {"is_group": 0})
		self.assertEqual(response["status"], "success")
		self.assertEqual(response["is_group"], 0)
		self.get_doc.assert_called_once_with("Item Group", "Test Group", for_update=True)
		self.doc.save.assert_called_once_with(ignore_permissions=True)

	def test_bare_json_and_json_string_payloads_are_supported(self):
		request = MagicMock(is_json=True)
		request.get_json.return_value = {"item_group": "Test Group", "image": "/files/test.png"}
		with patch.object(frappe, "request", request):
			self.assertEqual(api.update_item_group_from_crm("Test Group")["status"], "success")
		self.assertEqual(api.create_item_group_from_crm(frappe.as_json(self.payload))["status"], "success")

	def test_create_accepts_bare_json_body(self):
		request = MagicMock(is_json=True)
		request.get_json.return_value = self.payload
		with patch.object(frappe, "request", request):
			self.assertEqual(api.create_item_group_from_crm()["status"], "success")
		self.get_doc.assert_called_once_with({"doctype": "Item Group", **self.payload})

	def test_non_object_payloads_are_rejected_without_reading_or_writing_groups(self):
		for value in ([self.payload], "invalid", None, 123):
			request = MagicMock(is_json=True)
			request.get_json.return_value = value
			with patch.object(frappe, "request", request):
				with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
					api.create_item_group_from_crm()
		self.exists.assert_not_called()
		self.get_doc.assert_not_called()
		self.log.assert_not_called()

	def test_save_failure_is_propagated_and_logged_without_renaming(self):
		self.doc.save.side_effect = frappe.ValidationError("Native validation failed")
		with self.assertRaises(frappe.ValidationError):
			api.update_item_group_from_crm("Test Group", {"item_group_name": "Renamed Group"})
		self.rename.assert_not_called()
		self.assertEqual(self.update_log.call_args.kwargs["status"], "Failed")

	def test_only_post_is_registered(self):
		for method in (api.create_item_group_from_crm, api.update_item_group_from_crm):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
