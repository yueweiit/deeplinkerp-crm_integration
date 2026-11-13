from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import api


class TestCRMItemQueryAPI(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.stack = self.enterContext(ExitStack())
		self.session = self.stack.enter_context(patch.object(frappe, "session", frappe._dict(user="ordinary@example.test")))
		self.crm_auth = self.stack.enter_context(patch.object(api, "validate_crm_api_user"))
		self.columns = {
			"Item": set(frappe.model.default_fields) | {
				"item_code", "item_name", "item_group", "stock_uom", "description", "brand", "disabled", "image",
				"custom_dpci", "custom_external_code", "custom_sku", "custom_short_name", "custom_item_short_name",
			},
			"Item Group": set(frappe.model.default_fields) | {
				"item_group_name", "parent_item_group", "is_group", "image", "lft", "rgt",
			},
		}
		self.metas = {}
		for doctype in self.columns:
			meta = MagicMock()
			meta.has_field.side_effect = lambda fieldname, doctype=doctype: fieldname in self.columns[doctype]
			self.metas[doctype] = meta
		self.meta = self.stack.enter_context(patch.object(frappe, "get_meta", side_effect=self.metas.get))
		self.has_column = self.stack.enter_context(patch.object(
			frappe.db, "has_column", side_effect=lambda doctype, name: name in self.columns[doctype],
		))
		self.get_all = self.stack.enter_context(patch.object(frappe, "get_all", return_value=[]))
		self.doc = MagicMock()
		self.doc.as_dict.return_value = {"name": "ITEM-001", "custom_future_field": "value", "uoms": [{"uom": "Nos"}]}
		self.get_doc = self.stack.enter_context(patch.object(frappe, "get_doc", return_value=self.doc))
		self.log = self.stack.enter_context(patch.object(api, "create_crm_log"))

	def assert_no_master_queries(self):
		# 翻译缓存未命中时，错误提示可以读取 Translation；不得读取业务主数据。
		self.assertFalse([
			call for call in self.get_all.call_args_list
			if (call.args[0] if call.args else call.kwargs.get("doctype")) in self.columns
		])

	def test_authenticated_account_can_query_without_crm_identity_or_read_roles(self):
		with patch.object(frappe, "has_permission", return_value=False):
			self.assertEqual(api.query_items()["status"], "success")
			self.assertEqual(api.query_item_groups()["status"], "success")
		self.crm_auth.assert_not_called()
		self.log.assert_not_called()

	def test_guests_and_missing_identity_are_rejected_before_queries(self):
		for user in ("Guest", "", None):
			self.session.user = user
			for method in (api.query_items, api.query_item_groups):
				with self.subTest(user=user, method=method.__name__), self.assertRaises(frappe.AuthenticationError):
					method()
		self.meta.assert_not_called()
		self.assert_no_master_queries()
		self.get_doc.assert_not_called()

	def test_item_list_defaults_are_bounded_and_do_not_load_each_document(self):
		self.get_all.return_value = [{"name": "ITEM-001"}, {"name": "ITEM-002"}]
		response = api.query_items()
		self.assertEqual(response["data"], self.get_all.return_value)
		self.assertEqual((response["page"], response["page_size"], response["has_more"], response["next_page"]), (1, 20, False, None))
		self.assertEqual(self.get_all.call_args.args, ("Item",))
		self.assertEqual(self.get_all.call_args.kwargs["limit_page_length"], 21)
		self.assertEqual(self.get_all.call_args.kwargs["limit_start"], 0)
		self.assertEqual(self.get_all.call_args.kwargs["order_by"], "name asc")
		self.assertEqual(self.get_all.call_args.kwargs["filters"], {})
		self.get_doc.assert_not_called()

	def test_extra_row_sets_has_more_and_is_not_returned(self):
		self.get_all.return_value = [{"name": f"ITEM-{index}"} for index in range(3)]
		response = api.query_items(page="2", page_size="2")
		self.assertEqual(response["data"], self.get_all.return_value[:2])
		self.assertTrue(response["has_more"])
		self.assertEqual(response["next_page"], 3)
		self.assertEqual(self.get_all.call_args.kwargs["limit_start"], 2)
		self.assertEqual(self.get_all.call_args.kwargs["limit_page_length"], 3)

	def test_exact_size_page_does_not_claim_another_page(self):
		self.get_all.return_value = [{"name": "A"}, {"name": "B"}]
		response = api.query_item_groups(page_size=2)
		self.assertFalse(response["has_more"])
		self.assertIsNone(response["next_page"])

	def test_empty_or_missing_exact_match_returns_empty_data(self):
		for method, kwargs in ((api.query_items, {"item_code": "MISSING"}), (api.query_item_groups, {"item_group": "MISSING"})):
			with self.subTest(method=method.__name__):
				response = method(**kwargs)
				self.assertEqual(response["data"], [])
				self.assertFalse(response["has_more"])
		self.get_doc.assert_not_called()

	def test_exact_item_query_returns_full_model_and_child_tables(self):
		self.get_all.return_value = [{"name": "ITEM-001"}]
		response = api.query_items(item_code=" ITEM-001 ")
		self.assertEqual(self.get_all.call_args.kwargs["filters"], {"name": "ITEM-001"})
		self.get_doc.assert_called_once_with("Item", "ITEM-001")
		self.doc.as_dict.assert_called_once_with(no_private_properties=True)
		self.assertEqual(response["data"], [self.doc.as_dict.return_value])

	def test_exact_group_query_returns_native_details(self):
		self.get_all.return_value = [{"name": "Group A"}]
		api.query_item_groups(item_group="Group A")
		self.get_doc.assert_called_once_with("Item Group", "Group A")
		self.doc.as_dict.assert_called_once_with(no_private_properties=True)

	def test_item_filters_and_keyword_are_composed_without_freeform_filter_input(self):
		api.query_items(item_group="Products", disabled="0", keyword="SKU")
		kwargs = self.get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"], {"item_group": "Products", "disabled": 0})
		self.assertEqual(kwargs["or_filters"]["item_code"], ["like", "%SKU%"])
		self.assertEqual(kwargs["or_filters"]["custom_sku"], ["like", "%SKU%"])
		self.assertNotIn("custom_mnemonic_code", kwargs["or_filters"])

	def test_missing_optional_metadata_or_column_is_not_selected_or_searched(self):
		self.has_column.side_effect = lambda doctype, name: name in self.columns[doctype] and name != "custom_dpci"
		api.query_items(keyword="DPCI")
		kwargs = self.get_all.call_args.kwargs
		self.assertNotIn("custom_dpci", kwargs["fields"])
		self.assertNotIn("custom_dpci", kwargs["or_filters"])
		self.assertNotIn("custom_specifications", kwargs["fields"])

	def test_groups_default_to_all_types_and_tree_order(self):
		api.query_item_groups()
		self.assertEqual(self.get_all.call_args.kwargs["filters"], {})
		self.assertEqual(self.get_all.call_args.kwargs["order_by"], "lft asc, name asc")

	def test_group_parent_type_and_name_filters(self):
		api.query_item_groups(parent_item_group="Root", is_group=False, keyword="Group")
		kwargs = self.get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"], {"parent_item_group": "Root", "is_group": 0})
		self.assertEqual(kwargs["or_filters"], {"name": ["like", "%Group%"], "item_group_name": ["like", "%Group%"]})

	def test_empty_parent_queries_roots_without_treating_null_as_a_root_filter(self):
		api.query_item_groups(parent_item_group="")
		self.assertEqual(self.get_all.call_args.kwargs["filters"], {"parent_item_group": ["is", "not set"]})

	def test_zero_one_flags_accept_boolean_integer_and_get_string(self):
		for value in (False, 0, "0", True, 1, "1"):
			with self.subTest(value=value):
				api.query_items(disabled=value)
				self.assertEqual(self.get_all.call_args.kwargs["filters"]["disabled"], int(value))

	def test_invalid_pagination_values_are_rejected_before_reading(self):
		for kwargs in ({"page": 0}, {"page": -1}, {"page": True}, {"page": {}}, {"page": "1.0"}, {"page": 2_147_483_648}, {"page_size": 0}, {"page_size": 101}, {"page_size": 1.5}, {"page_size": ""}, {"page_size": None}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.ValidationError):
				api.query_items(**kwargs)
		self.assert_no_master_queries()

	def test_page_size_hundred_fetches_at_most_one_hundred_and_one_rows(self):
		api.query_item_groups(page_size=100)
		self.assertEqual(self.get_all.call_args.kwargs["limit_page_length"], 101)

	def test_invalid_string_inputs_cannot_become_database_filter_objects(self):
		for kwargs in ({"item_code": {"name": ["like", "%"]}}, {"item_code": []}, {"item_code": " "}, {"item_group": 123}, {"keyword": True}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.ValidationError):
				api.query_items(**kwargs)
		for kwargs in ({"item_group": {}}, {"item_group": ""}, {"parent_item_group": []}, {"keyword": {}}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.ValidationError):
				api.query_item_groups(**kwargs)
		self.assert_no_master_queries()

	def test_invalid_flags_are_rejected(self):
		for value in (2, -1, 1.0, "true", "", [], {}):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				api.query_items(disabled=value)
			with self.subTest(value=value, group=True), self.assertRaises(frappe.ValidationError):
				api.query_item_groups(is_group=value)
		self.assert_no_master_queries()

	def test_blank_keyword_is_an_unfiltered_list(self):
		api.query_items(keyword="  ")
		self.assertEqual(self.get_all.call_args.kwargs["or_filters"], {})

	def test_database_errors_are_propagated(self):
		self.get_all.side_effect = RuntimeError("query failed")
		with self.assertRaises(RuntimeError):
			api.query_item_groups()

	def test_legacy_queries_still_require_crm_identity(self):
		self.crm_auth.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			api.get_item_group_list()
		with self.assertRaises(frappe.PermissionError):
			api.check_item_exists("ITEM-001")
		self.assert_no_master_queries()

	def test_both_get_and_post_are_registered(self):
		for method in (api.query_items, api.query_item_groups):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["GET", "POST"])
