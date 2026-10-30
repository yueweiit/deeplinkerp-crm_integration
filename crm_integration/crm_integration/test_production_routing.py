"""NSF release regressions use mocked external calls and no business-data writes."""

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import finance_release as review
from crm_integration.crm_integration import production_routing as routing
from crm_integration.crm_integration import sales_order as flow


def order(*codes, status=flow.DEPOSIT_CONFIRMATION_PROCESSING):
	doc = MagicMock()
	doc.name, doc.company, doc.docstatus = "NSF-TEST-SO", "TEST COMPANY", 1
	doc.modified = "2026-10-30 12:00:00"
	values = frappe._dict(company=doc.company, custom_crm_order_no="NSF-TEST-CRM", custom_process_status=status,
		custom_crm_production_started=0, status="To Deliver and Bill",
		items=[frappe._dict(item_code=code, qty=10) for code in codes])
	doc.get.side_effect = values.get
	return doc


class TestNSFProductionRouting(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch.object(routing, "get_company_direct_product_prefixes", return_value=frozenset({"NSF"})))

	def test_only_nonempty_all_nsf_positive_orders_are_direct(self):
		for codes, expected in (
			(("NSF4030112104", "NSF4030117104"), routing.DIRECT),
			((" nsf4030112104 ",), routing.DIRECT),
			(("NBD4030000101",), routing.MES),
			(("UNKNOWN",), routing.MES), ((None,), routing.MES), ((), routing.MES),
			(("NSF4030112104", "NBD4030000101"), routing.MIXED),
			(("NBD4030000101", "NSF4030112104"), routing.MIXED),
		):
			with self.subTest(codes=codes):
				self.assertEqual(routing.get_production_route(order(*codes)), expected)
		for qty in (0, -1, None, "invalid"):
			with self.subTest(qty=qty):
				self.assertFalse(routing.is_direct_fulfillment_order(frappe._dict(items=[frappe._dict(item_code="NSF4030112104", qty=qty)])))

	def test_direct_release_keeps_crm_events_and_production_lock_but_waits_for_final_payment(self):
		doc = order("NSF4030112104", "NSF4030117104")
		with (
			patch.object(flow, "push_sales_order_status_to_crm") as crm,
			patch.object(flow, "push_sales_order_to_mes") as mes,
			patch.object(flow, "set_process_status") as status,
			patch.object(frappe.db, "commit") as commit,
		):
			result = flow.run_confirm_deposit_sync(doc)
		self.assertEqual(crm.call_args_list, [call(doc, flow.CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION), call(doc, flow.CRM_STATUS_IN_PRODUCTION)])
		mes.assert_not_called()
		status.assert_called_once_with(doc, flow.PENDING_FINAL_PAYMENT)
		self.assertEqual(result["fulfillment_route"], routing.DIRECT)
		self.assertEqual(result["process_status"], flow.PENDING_FINAL_PAYMENT)
		self.assertEqual(doc.custom_crm_production_started, 1)
		self.assertEqual(doc.custom_crm_production_milestone, "REACHED")
		self.assertEqual(commit.call_count, 2)

	def test_existing_e3_confirmed_nsf_failure_can_retry_without_mes_or_clearing_lock(self):
		doc = order("NSF4030112104")
		original_get = doc.get.side_effect
		doc.get.side_effect = lambda field: 1 if field == "custom_crm_production_started" else original_get(field)
		with (
			patch.object(flow, "push_sales_order_status_to_crm") as crm,
			patch.object(flow, "push_sales_order_to_mes") as mes,
			patch.object(flow, "set_process_status") as status,
			patch.object(frappe.db, "commit") as commit,
		):
			flow.run_confirm_deposit_sync(doc)
		self.assertEqual(crm.call_count, 2)
		mes.assert_not_called()
		doc.db_set.assert_called_once_with(routing.ORDER_ROUTE_FIELD, routing.DIRECT, update_modified=False)
		commit.assert_not_called()
		status.assert_called_once_with(doc, flow.PENDING_FINAL_PAYMENT)

	def test_direct_final_payment_state_replays_without_external_calls(self):
		doc = order("NSF4030112104", status=flow.PENDING_FINAL_PAYMENT)
		with patch.object(flow, "push_sales_order_status_to_crm") as crm, patch.object(flow, "push_sales_order_to_mes") as mes, patch.object(flow, "set_process_status") as status:
			result = flow.run_confirm_deposit_sync(doc)
		self.assertTrue(result["idempotent_replay"])
		self.assertEqual(result["process_status"], flow.PENDING_FINAL_PAYMENT)
		crm.assert_not_called(); mes.assert_not_called(); status.assert_not_called()

	def test_crm_failure_does_not_release_nsf_order(self):
		doc = order("NSF4030112104")
		with (
			patch.object(flow, "push_sales_order_status_to_crm", side_effect=RuntimeError("CRM unavailable")),
			patch.object(flow, "push_sales_order_to_mes") as mes,
			patch.object(flow, "set_process_status") as status,
			self.assertRaises(RuntimeError),
		):
			flow.run_confirm_deposit_sync(doc)
		mes.assert_not_called(); status.assert_not_called()

	def test_mixed_order_fails_before_crm_mes_or_status_changes(self):
		doc = order("NSF4030112104", "NBD4030000101")
		with (
			patch.object(flow, "push_sales_order_status_to_crm") as crm,
			patch.object(flow, "push_sales_order_to_mes") as mes,
			patch.object(flow, "set_process_status") as status,
			self.assertRaises(frappe.ValidationError),
		):
			flow.run_confirm_deposit_sync(doc)
		crm.assert_not_called(); mes.assert_not_called(); status.assert_not_called(); doc.db_set.assert_not_called()

	def test_nsf_e3_timeout_retains_attempt_marker_without_marking_financial_release_complete(self):
		doc = order("NSF4030112104")
		with (
			patch.object(flow, "push_sales_order_status_to_crm", side_effect=[None, RuntimeError("E3 timeout")]),
			patch.object(flow, "push_sales_order_to_mes") as mes,
			patch.object(flow, "set_process_status") as status,
			patch.object(frappe.db, "commit") as commit,
			self.assertRaises(RuntimeError),
		):
			flow.run_confirm_deposit_sync(doc)
		doc.db_set.assert_called_once_with("custom_crm_production_milestone", "ATTEMPTED", update_modified=True)
		commit.assert_called_once_with()
		status.assert_not_called(); mes.assert_not_called()

	def test_mixed_preflight_cannot_queue_even_if_a_caller_supplies_an_outdated_review(self):
		doc = order("NSF4030112104", "NBD4030000101", status=flow.PENDING_DEPOSIT_CONFIRMATION)
		with (
			patch.object(frappe.db, "get_value"),
			patch.object(review, "read_order", return_value=doc),
			patch.object(review, "review_order", return_value={"can_release": True}),
			patch.object(review, "has_release_permission", return_value=True),
			patch.object(flow, "is_crm_integration_enabled", return_value=True),
			patch.object(flow, "create_crm_log") as audit,
			patch.object(flow, "enqueue_confirm_deposit_and_push_to_mes") as queue,
			self.assertRaises(frappe.ValidationError),
		):
			flow.confirm_deposit_and_push_to_mes(doc.name)
		audit.assert_not_called(); queue.assert_not_called()

	def test_preflight_skips_mes_configuration_but_keeps_financial_audit_and_queue(self):
		doc = order("NSF4030112104", status=flow.PENDING_DEPOSIT_CONFIRMATION)
		with (
			patch.object(frappe.db, "get_value"),
			patch.object(review, "read_order", return_value=doc),
			patch.object(review, "review_order", return_value={"can_release": True, "receipts": [], "receipts_complete": True}),
			patch.object(review, "has_release_permission", return_value=True),
			patch.object(flow, "is_crm_integration_enabled", return_value=True),
			patch.object(flow, "validate_mes_sync_available") as config,
			patch.object(flow, "create_crm_log", return_value=frappe._dict(name="TEST-AUDIT")) as audit,
			patch.object(flow, "set_process_status") as status,
			patch.object(flow, "enqueue_confirm_deposit_and_push_to_mes") as queue,
		):
			result = flow.confirm_deposit_and_push_to_mes(doc.name)
		config.assert_not_called()
		status.assert_called_once_with(doc, flow.DEPOSIT_CONFIRMATION_PROCESSING)
		queue.assert_called_once_with(doc.name, "TEST-AUDIT")
		self.assertTrue(result["queued"])
		self.assertFalse(audit.call_args.kwargs["request_payload"]["mes_required"])

	def test_direct_low_level_mes_push_is_skipped_before_configuration_and_network(self):
		doc = order("NSF4030112104")
		with patch.object(flow, "validate_mes_sync_available") as config, patch.object(flow.requests, "post") as post:
			self.assertEqual(flow.push_sales_order_to_mes(doc)["reason"], "NO_MES_PRODUCTION")
		config.assert_not_called(); post.assert_not_called()

	def test_review_marks_mixed_order_ineligible_and_exposes_route_only_with_item_read_access(self):
		doc = order("NSF4030112104", "NBD4030000101", status=flow.PENDING_DEPOSIT_CONFIRMATION)
		with (
			patch.object(review, "readable_fields", return_value={"items", "item_code", "qty"}),
			patch.object(review, "recorded_receipts", return_value={"receipts": [], "receipts_complete": True, "association_error": False}),
			patch.object(review, "is_crm_integration_enabled", return_value=True),
			patch.object(review, "has_release_permission", return_value=True),
			patch.object(review, "reject_reason", return_value=""),
			patch.object(frappe.db, "exists", return_value=False),
		):
			result = review.review_order(doc)
			self.assertFalse(result["can_release"])
			self.assertEqual(result["fulfillment_route"], routing.MIXED)
			with patch.object(review, "readable_fields", return_value=set()):
				self.assertNotIn("fulfillment_route", review.review_order(doc))


class TestDirectProductConfiguration(UnitTestCase):
	def test_prefix_list_normalizes_case_separators_and_duplicates(self):
		self.assertEqual(routing.parse_direct_product_prefixes(" nsf\nabc, NSF；nha "), frozenset({"NSF", "ABC", "NHA"}))
		self.assertEqual(routing.parse_direct_product_prefixes(" \n "), frozenset())
		self.assertEqual(routing.parse_direct_product_prefixes(None), frozenset())

	def test_invalid_short_long_and_wildcard_rules_are_rejected(self):
		for value in ("N", "NS", "NSF4030", "NS*", "*", "NSF/ABC", ["NSF"], 123):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				routing.parse_direct_product_prefixes(value)

	def test_company_rules_are_independent_and_empty_list_disables_direct_products(self):
		with patch.object(frappe.db, "has_column", return_value=True), patch.object(frappe.db, "get_value", side_effect=lambda doctype, company, field: "NSF\nABC" if company == "MX" else "") as query:
			self.assertEqual(routing.get_company_direct_product_prefixes("MX"), frozenset({"NSF", "ABC"}))
			self.assertEqual(routing.get_company_direct_product_prefixes("CN"), frozenset())
		self.assertTrue(all(call.args[2] == routing.DIRECT_PRODUCT_FIELD for call in query.call_args_list))
		with patch.object(frappe.db, "has_column", return_value=False), patch.object(frappe.db, "get_value") as query:
			self.assertEqual(routing.get_company_direct_product_prefixes("MX"), frozenset({"NSF"}))
		query.assert_not_called()

	def test_new_product_is_configured_once_for_all_models_and_colors(self):
		doc = frappe._dict(company="MX", items=[frappe._dict(item_code="ABC4030000101", qty=2, color="Red"), frappe._dict(item_code="ABC4030099902", qty=3, color="Blue")])
		with patch.object(routing, "get_company_direct_product_prefixes", return_value=frozenset({"NSF", "ABC"})):
			self.assertEqual(routing.get_production_route(doc), routing.DIRECT)
			doc["items"].append(frappe._dict(item_code="NSF4030112104", qty=2))
			self.assertEqual(routing.get_production_route(doc), routing.MIXED)
		with patch.object(routing, "get_company_direct_product_prefixes", return_value=frozenset({"NSF"})):
			self.assertEqual(routing.get_production_route(frappe._dict(company="MX", items=doc["items"][:2])), routing.MES)

	def company(self, old, current):
		doc = MagicMock()
		doc.meta.has_field.return_value = True
		doc.get.return_value = current
		doc.get_doc_before_save.return_value = frappe._dict({routing.DIRECT_PRODUCT_FIELD: old})
		return doc

	def test_only_system_managers_can_change_company_product_scope(self):
		doc = self.company("NSF", "NSF\nABC")
		with patch.object(frappe, "session", frappe._dict(user="ordinary-user")), patch.object(frappe, "get_roles", return_value=["Accounts Manager"]), self.assertRaises(frappe.PermissionError):
			routing.validate_company_direct_products(doc)
		doc.set.assert_not_called()
		for user, roles in (("Administrator", []), ("manager", ["System Manager"])):
			with self.subTest(user=user), patch.object(frappe, "session", frappe._dict(user=user)), patch.object(frappe, "get_roles", return_value=roles):
				routing.validate_company_direct_products(doc)
		self.assertEqual(doc.set.call_args.args, (routing.DIRECT_PRODUCT_FIELD, "ABC\nNSF"))

	def test_normal_company_edits_do_not_require_product_configuration_privilege(self):
		doc = self.company("NSF\nABC", "abc, nsf, NSF")
		with patch.object(frappe, "session", frappe._dict(user="ordinary-user")), patch.object(frappe, "get_roles", return_value=["Accounts Manager"]):
			routing.validate_company_direct_products(doc)
		doc.set.assert_called_once_with(routing.DIRECT_PRODUCT_FIELD, "ABC\nNSF")

	def test_successful_route_snapshot_wins_over_later_company_rule_changes(self):
		doc = frappe._dict(name="TEST-SO", custom_crm_order_no="TEST-CRM", company="MX", items=[frappe._dict(item_code="ABC4030000101", qty=1)])
		for route in (routing.DIRECT, routing.MES):
			with (
				self.subTest(route=route),
				patch.object(frappe.db, "exists", return_value=True),
				patch.object(frappe.db, "get_value", return_value=frappe._dict(response_payload=frappe.as_json({"fulfillment_route": route}), request_payload="{}")) as query,
				patch.object(routing, "get_company_direct_product_prefixes") as config,
			):
				self.assertEqual(routing.get_effective_production_route(doc), route)
				self.assertEqual(query.call_args.args[1]["status"], "Success")
				config.assert_not_called()

	def test_existing_successful_mes_order_keeps_callbacks_when_product_is_added_later(self):
		doc = frappe._dict(name="TEST-SO", custom_crm_order_no="TEST-CRM")
		with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe.db, "get_value", return_value=None), patch.object(routing, "get_company_direct_product_prefixes") as config:
			self.assertEqual(routing.get_effective_production_route(doc), routing.MES)
		config.assert_not_called()

	def test_success_audit_persists_actual_worker_route_instead_of_preflight_guess(self):
		with patch.object(frappe.db, "get_value", return_value=(review.AUDIT_EVENT, "TEST-SO")), patch.object(frappe.db, "set_value") as write:
			review.update_finance_audit("TEST-AUDIT", "TEST-SO", "Success", flow.PENDING_FINAL_PAYMENT, fulfillment_route=routing.DIRECT)
		self.assertIn('"fulfillment_route": "DIRECT"', write.call_args.args[2]["response_payload"])

	def test_field_sync_only_initializes_new_columns_and_never_reenables_cleared_rules(self):
		from crm_integration.patches import add_direct_fulfillment_products as migration
		for exists in (False, True):
			with (
				self.subTest(exists=exists),
				patch.object(frappe.db, "has_column", return_value=exists),
				patch.object(migration, "create_custom_fields") as fields,
				patch.object(migration, "backfill_order_routes"),
				patch.object(frappe, "clear_cache"),
				patch.object(frappe.db, "sql", return_value=[]) as sql,
			):
				migration.execute()
				field = fields.call_args.args[0]["Company"][0]
				self.assertEqual(field["fieldname"], routing.DIRECT_PRODUCT_FIELD)
				self.assertEqual(field["default"], "NSF")
				self.assertIn("System Manager", field["read_only_depends_on"])
				if exists:
					sql.assert_not_called()
				else:
					sql.assert_called_once()
					self.assertIn("IS NULL", sql.call_args.args[0])
					self.assertIn(" OR ", sql.call_args.args[0])


class TestDurableProductionRoute(UnitTestCase):
	def test_rule_change_during_crm_sync_does_not_skip_required_mes_submission(self):
		doc = order("NSF4030112104")
		services = frappe._dict(create_log=MagicMock(), update_log=MagicMock())
		with (
			patch.object(routing, "get_company_direct_product_prefixes", side_effect=[frozenset(), frozenset({"NSF"})]) as config,
			patch.object(flow, "push_sales_order_status_to_crm"),
			patch.object(flow, "validate_mes_sync_available", return_value=services),
			patch.object(flow, "build_mes_sales_order_payload", return_value={"doc_name": doc.name}),
			patch.object(flow, "get_mes_sales_order_push_url", return_value="https://mes.invalid/order"),
			patch.object(flow, "get_mes_sales_order_push_authorization", return_value="test-authorization"),
			patch.object(flow, "parse_crm_response", return_value={"success": True}),
			patch.object(flow.requests, "post") as post,
			patch.object(frappe.db, "commit"),
			patch.object(flow, "set_process_status") as status,
		):
			result = flow.run_confirm_deposit_sync(doc)
		config.assert_called_once()
		post.assert_called_once()
		self.assertEqual(result["fulfillment_route"], routing.MES)
		status.assert_called_once_with(doc, flow.PENDING_PRODUCTION)
		self.assertEqual(doc.db_set.call_args.args, (routing.ORDER_ROUTE_FIELD, routing.MES))

	def test_direct_route_is_not_reclassified_when_product_is_removed_mid_sync(self):
		doc = order("NSF4030112104")
		with patch.object(routing, "get_company_direct_product_prefixes", side_effect=[frozenset({"NSF"}), frozenset()]) as config, patch.object(flow, "push_sales_order_status_to_crm"), patch.object(flow, "push_sales_order_to_mes") as mes, patch.object(frappe.db, "commit"), patch.object(flow, "set_process_status") as status:
			flow.run_confirm_deposit_sync(doc)
		config.assert_called_once()
		mes.assert_not_called()
		status.assert_called_once_with(doc, flow.PENDING_FINAL_PAYMENT)
		self.assertEqual(doc.db_set.call_args.args, (routing.ORDER_ROUTE_FIELD, routing.DIRECT))

	def test_skipped_or_invalid_mes_response_never_advances_order_or_stores_success_route(self):
		for response in ({"status": "skipped"}, None, {}, {"success": False}, {"success": "true"}):
			doc = order("NBD4030000101")
			with self.subTest(response=response), patch.object(routing, "get_company_direct_product_prefixes", return_value=frozenset()), patch.object(flow, "push_sales_order_status_to_crm"), patch.object(flow, "push_sales_order_to_mes", return_value=response), patch.object(frappe.db, "commit"), patch.object(flow, "set_process_status") as status, self.assertRaises(frappe.ValidationError):
				flow.run_confirm_deposit_sync(doc)
			status.assert_not_called()
			self.assertFalse(any(call.args[0] == routing.ORDER_ROUTE_FIELD for call in doc.db_set.call_args_list))

	def test_missing_snapshot_schema_stops_before_any_external_event(self):
		with patch.object(frappe.db, "has_column", return_value=False), patch.object(flow, "push_sales_order_status_to_crm") as crm, patch.object(flow, "push_sales_order_to_mes") as mes, self.assertRaises(frappe.ValidationError):
			flow.run_confirm_deposit_sync(order("NSF4030112104"))
		crm.assert_not_called(); mes.assert_not_called()

	def test_order_snapshot_survives_log_deletion_and_company_rule_changes(self):
		from mes_integration.mes_integration.settings import requires_sales_order_mes_sync
		for route in (routing.DIRECT, routing.MES):
			doc = frappe._dict(name="TEST-OLD-SO", docstatus=1, custom_crm_order_no="CRM-OLD",
				custom_process_status="Deliverable", **{routing.ORDER_ROUTE_FIELD: route})
			with self.subTest(route=route), patch.object(frappe.db, "exists", return_value=False), patch.object(frappe.db, "get_value") as logs, patch.object(routing, "get_company_direct_product_prefixes") as config:
				self.assertEqual(requires_sales_order_mes_sync(doc), route == routing.MES)
			logs.assert_not_called(); config.assert_not_called()

	def test_legacy_pending_production_keeps_mes_route_without_any_logs(self):
		doc = frappe._dict(name="OLD-MES-SO", docstatus=1, custom_crm_order_no="OLD-CRM", custom_process_status="Pending Production")
		with patch.object(frappe.db, "exists", return_value=False), patch.object(routing, "get_company_direct_product_prefixes") as config:
			self.assertEqual(routing.get_effective_production_route(doc), routing.MES)
		config.assert_not_called()

	def test_unknown_historical_route_is_not_guessed_from_current_product_list(self):
		from mes_integration.mes_integration import sales_order as callbacks
		doc = frappe._dict(name="OLD-UNKNOWN-SO", docstatus=1, custom_crm_order_no="OLD-CRM", custom_process_status="Pending Final Payment")
		with patch.object(frappe.db, "exists", return_value=False), patch.object(routing, "get_company_direct_product_prefixes") as config, patch.object(callbacks, "post_stock_entry_status_to_mes") as post:
			self.assertEqual(routing.get_effective_production_route(doc), routing.UNKNOWN)
			with self.assertRaises(frappe.ValidationError):
				callbacks.push_sales_order_status_doc_to_mes(doc)
		config.assert_not_called(); post.assert_not_called()

	def test_snapshot_cannot_be_injected_or_changed_through_normal_save(self):
		doc = MagicMock()
		for previous, current in ((None, routing.DIRECT), (routing.MES, routing.DIRECT), (routing.UNKNOWN, "")):
			doc.get.return_value = current
			doc.get_doc_before_save.return_value = frappe._dict({routing.ORDER_ROUTE_FIELD: previous}) if previous else None
			with self.subTest(previous=previous, current=current), self.assertRaises(frappe.PermissionError):
				routing.protect_recorded_production_route(doc)
		doc.get.return_value = routing.MES
		doc.get_doc_before_save.return_value = frappe._dict({routing.ORDER_ROUTE_FIELD: routing.MES})
		routing.protect_recorded_production_route(doc)

	def test_bounded_backfill_uses_evidence_not_current_rules_and_preserves_existing_snapshots(self):
		from crm_integration.patches import add_direct_fulfillment_products as migration
		rows = [frappe._dict(name=name, docstatus=1, custom_process_status=status) for name, status in (
			("A", "Pending Final Payment"), ("B", "Deliverable"), ("C", "Pending Production"),
			("D", "Completed"), ("E", "Pending Deposit Confirmation"),
		)]
		audits = [frappe._dict(reference_name="A", status="Success", response_payload='{"fulfillment_route":"DIRECT"}', request_payload="{}")]
		with patch.object(frappe, "get_all", side_effect=[rows, [frappe._dict(reference_name="B")], []]) as read, patch.object(frappe.db, "exists", return_value=True), patch.object(review, "latest_order_logs", return_value=audits) as audit, patch.object(frappe.db, "sql", return_value=[]) as write, patch.object(routing, "get_company_direct_product_prefixes") as config:
			migration.backfill_order_routes(batch_size=5)
		self.assertEqual(read.call_args_list[0].kwargs["limit"], 5)
		self.assertEqual(read.call_args_list[-1].kwargs["filters"]["name"], [">", "E"])
		self.assertEqual(audit.call_args.kwargs["status"], "Success")
		self.assertEqual(write.call_count, 3)
		for call in write.call_args_list:
			self.assertIn("IS NULL", call.args[0])
			self.assertIn(" OR ", call.args[0])
		params = [value for call in write.call_args_list for value in call.args[1].values()]
		self.assertTrue({"A", "B", "C", "D", routing.DIRECT, routing.MES, routing.UNKNOWN} <= set(params))
		self.assertNotIn("E", params)
		config.assert_not_called()
