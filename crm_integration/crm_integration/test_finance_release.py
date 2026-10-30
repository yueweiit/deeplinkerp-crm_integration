from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import finance_release as release
from crm_integration.crm_integration import sales_order as flow


class TestFinanceRelease(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch("crm_integration.crm_integration.production_routing.get_company_direct_product_prefixes", return_value=frozenset({"NSF"})))
		self.doc = frappe._dict(doctype="Sales Order", name="SO-1", company="MX", customer="C-1", custom_crm_order_no="CRM-1", docstatus=1,
			status="To Deliver and Bill", custom_process_status=release.PENDING)
		self.enterContext(patch.object(release, "readable_fields", return_value={"company", "customer", "custom_process_status", "references", "party_type", "party", "payment_type", "posting_date", "paid_from_account_currency", "paid_to_account_currency", "reference_doctype", "reference_name", "allocated_amount"}))
		self.enterContext(patch.object(release, "has_release_permission", return_value=True))
		self.enterContext(patch.object(release, "is_crm_integration_enabled", return_value=True))
		self.enterContext(patch.object(frappe.db, "exists", return_value=False))
		self.enterContext(patch.object(frappe, "get_all", return_value=[]))

	def test_manual_authorization_does_not_require_a_recorded_deposit(self):
		review = release.review_order(self.doc)
		self.assertTrue(review["can_release"])
		self.assertEqual(review["receipts"], [])
		self.assertNotIn("required_deposit", review)

	def test_reject_action_requires_cancel_permission_and_pending_submitted_order(self):
		with patch.object(frappe, "has_permission", return_value=True):
			self.assertTrue(release.review_order(self.doc)["can_reject"])
			for values in ({"docstatus": 0}, {"status": "Closed"}, {"custom_process_status": release.PROCESSING}):
				with self.subTest(values=values):
					self.assertFalse(release.review_order(frappe._dict({**self.doc, **values}))["can_reject"])
		with patch.object(frappe, "has_permission", return_value=False):
			review = release.review_order(self.doc)
			self.assertFalse(review["can_reject"])
			self.assertEqual(review["reject_reason"], frappe._("没有取消此销售订单的权限，无法驳回。"))
		with patch.object(release, "is_crm_integration_enabled", return_value=False), patch.object(frappe, "has_permission", return_value=True):
			self.assertFalse(release.review_order(self.doc)["can_reject"])
		with patch.object(frappe, "has_permission", return_value=True):
			review = release.review_order(frappe._dict({**self.doc, "custom_crm_order_no": None}))
			self.assertFalse(review["can_reject"])
			self.assertEqual(review["reject_reason"], frappe._("缺少 CRM 订单编号，无法推送状态到 CRM。"))

	def test_reject_permission_is_independent_of_production_release_permission(self):
		with patch.object(release, "has_release_permission", return_value=False), patch.object(frappe, "has_permission", return_value=True):
			review = release.review_order(self.doc)
		self.assertFalse(review["can_release"])
		self.assertTrue(review["can_reject"])

	def test_sales_order_edit_permission_does_not_grant_release(self):
		with patch.object(release, "has_release_permission", return_value=False):
			self.assertFalse(release.review_order(self.doc)["can_release"])

	def test_current_state_is_checked_independently_of_finance_authorization(self):
		for values in ({"docstatus": 0}, {"docstatus": 2}, {"status": "Closed"}, {"status": "On Hold"}, {"custom_process_status": release.PROCESSING}, {"custom_process_status": "Pending Production"}):
			with self.subTest(values=values):
				self.assertFalse(release.review_order(frappe._dict({**self.doc, **values}))["can_release"])

	def test_disabled_crm_company_cannot_release(self):
		with patch.object(release, "is_crm_integration_enabled", return_value=False):
			self.assertFalse(release.review_order(self.doc)["can_release"])

	def test_order_release_capability_does_not_disclose_unreadable_confirmation_history(self):
		with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe, "has_permission", return_value=False), patch.object(frappe, "get_list") as logs:
			review = release.review_order(self.doc)
		self.assertTrue(review["can_release"])
		self.assertIsNone(review["last_confirmation"])
		logs.assert_not_called()

	def test_confirmation_history_uses_native_list_and_field_read_permissions(self):
		with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe, "has_permission", return_value=True), patch.object(release, "readable_fields", return_value={"user", "creation", "status"}), patch.object(frappe, "get_list", return_value=[{"user": "visible-user", "creation": "time", "status": "Pending"}]) as logs:
			review = release.review_order(self.doc)
		self.assertEqual(review["last_confirmation"]["user"], "visible-user")
		self.assertEqual(logs.call_args.kwargs["filters"]["reference_name"], "SO-1")

	def payment(self, **values):
		return frappe._dict(name="PE-1", company="MX", party_type="Customer", party="C-1", docstatus=1, payment_type="Receive", posting_date="2026-10-04", paid_from_account_currency="MXN",
			references=[frappe._dict(reference_doctype="Sales Order", reference_name="SO-1", allocated_amount=123)], **values)

	def test_receipts_show_allocation_not_payment_total_and_keep_currency(self):
		payment = self.payment()
		payment.references.append(frappe._dict(reference_doctype="Sales Order", reference_name="SO-OTHER", allocated_amount=900))
		with patch.object(frappe, "get_all", return_value=["PE-1"]), patch.object(frappe, "get_doc", return_value=payment), patch.object(frappe, "has_permission", return_value=True):
			row = release.recorded_receipts(self.doc)["receipts"][0]
			self.assertEqual(row["allocated_amount"], 123)
			self.assertEqual(row["currency"], "MXN")

	def test_hidden_receipt_is_omitted_without_treating_it_as_nonpayment(self):
		with patch.object(frappe, "get_all", return_value=["SECRET"]), patch.object(frappe, "get_doc", return_value=self.payment()), patch.object(frappe, "has_permission", return_value=False):
			review = release.review_order(self.doc)
			self.assertTrue(review["can_release"])
			self.assertFalse(review["receipts_complete"])
			self.assertNotIn("SECRET", str(review))

	def test_wrong_company_or_customer_association_blocks_confirmation(self):
		for field in ("company", "party"):
			payment = self.payment(); payment[field] = "OTHER"
			with self.subTest(field=field), patch.object(frappe, "get_all", return_value=["PE-1"]), patch.object(frappe, "get_doc", return_value=payment), patch.object(frappe, "has_permission", return_value=True):
				self.assertFalse(release.review_order(self.doc)["can_release"])

	def test_failed_batch_item_does_not_undo_committed_orders(self):
		with patch.object(flow, "confirm_deposit_and_push_to_mes", side_effect=[{"queued": True}, RuntimeError(), {"queued": True}]) as confirm, patch.object(frappe.db, "commit") as commit, patch.object(frappe.db, "rollback") as rollback:
			rows = release.confirm_production_release_batch(["SO-1", "SO-2", "SO-3"])["orders"]
		self.assertEqual([row["state"] for row in rows], ["processing", "failed", "processing"])
		self.assertEqual(commit.call_count, 2); rollback.assert_called_once()
		self.assertEqual(confirm.call_count, 3)

	def test_unscoped_order_review_returns_no_details(self):
		with patch.object(release, "read_order", side_effect=frappe.PermissionError):
			row = release.get_finance_release_review(["SO-HIDDEN"])["orders"][0]
		self.assertEqual(set(row), {"name", "can_release", "can_reject", "reason", "reject_reason"})

	def test_latest_failed_mes_push_exposes_only_an_actionable_summary(self):
		rows = [{"name": "SO-1", "process_status": release.PENDING, "sync_error": None}]
		audit = frappe._dict(reference_name="SO-1", creation="2026-10-26 10:00:00", status="Failed")
		log = frappe._dict(reference_name="SO-1", creation="2026-10-26 10:01:00", status="Failed", http_status_code="409",
			response_payload='{"errorCode":"ERP_SALES_ORDER_IDENTITY_CONFLICT","message":"private details <script>"}')
		with patch.object(frappe.db, "exists", return_value=True), patch.object(release, "latest_order_logs", side_effect=[[audit], [log]]) as query:
			release.attach_sync_errors(rows)
		self.assertEqual(rows[0]["sync_error"]["code"], "ERP_SALES_ORDER_IDENTITY_CONFLICT")
		self.assertEqual(rows[0]["sync_status"], "Failed")
		self.assertTrue(rows[0]["sync_error"]["message"])
		self.assertNotIn("private details", str(rows[0]))
		self.assertEqual(query.call_count, 2)
		self.assertEqual(query.call_args.kwargs["since"], audit.creation)

	def test_sync_summary_ignores_older_failure_after_a_later_success(self):
		rows = [{"name": "SO-1", "process_status": release.PENDING, "sync_error": None}]
		audit = frappe._dict(reference_name="SO-1", creation="2026-10-26 10:00:00", status="Failed")
		latest = frappe._dict(reference_name="SO-1", creation="2026-10-26 10:02:00", status="Success", http_status_code="200", response_payload="{}")
		with patch.object(frappe.db, "exists", return_value=True), patch.object(release, "latest_order_logs", side_effect=[[audit], [latest]]):
			release.attach_sync_errors(rows)
		self.assertIsNone(rows[0]["sync_error"])
		self.assertEqual(rows[0]["sync_status"], "Failed")

	def test_sync_summary_requires_release_permission_and_readable_order(self):
		rows = [{"name": "SO-HIDDEN", "sync_error": None}]
		with patch.object(frappe.db, "exists", return_value=True), patch.object(release, "latest_order_logs") as query:
			release.attach_sync_errors(rows)
		query.assert_not_called()
		with patch.object(release, "has_release_permission", return_value=False), patch.object(frappe.db, "exists", return_value=True), patch.object(release, "latest_order_logs") as query:
			release.attach_sync_errors([{"name": "SO-1", "process_status": release.PENDING, "sync_error": None}])
		query.assert_not_called()

	def test_release_operator_sees_only_safe_failure_state_without_log_read_permission(self):
		with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe, "has_permission", return_value=False), patch.object(frappe, "get_list") as readable_logs:
			row = release.review_order(self.doc)
			with patch.object(release, "latest_order_logs", side_effect=[[frappe._dict(reference_name="SO-1", creation="2026-10-26 10:00:00", status="Failed")], []]) as query:
				release.attach_sync_errors([row])
		self.assertIsNone(row["last_confirmation"])
		self.assertEqual(row["sync_status"], "Failed")
		self.assertIsNone(row["sync_error"])
		readable_logs.assert_not_called()
		self.assertEqual(query.call_count, 2)

	def test_generic_mes_error_code_is_visible_without_raw_response(self):
		log = frappe._dict(response_payload='{"errorCode":"ORDER_INVALID","message":"private payload"}', http_status_code="422")
		summary = release.mes_push_failure_summary(log)
		self.assertEqual(summary["code"], "ORDER_INVALID")
		self.assertIn("ORDER_INVALID", summary["message"])
		self.assertNotIn("private payload", str(summary))
		invalid = release.mes_push_failure_summary(frappe._dict(response_payload='{"errorCode":"<script>"}', http_status_code="500"))
		self.assertEqual(invalid["code"], "HTTP 500")
		self.assertIn("HTTP 500", invalid["message"])

	def test_latest_log_query_accepts_the_set_and_mapping_used_by_review(self):
		name = "__finance_release_missing_order__"
		for names in ({name}, {name: True}):
			with self.subTest(names=type(names).__name__):
				self.assertEqual(release.latest_order_logs("CRM Integration Log", release.AUDIT_EVENT, names,
					("reference_name", "creation", "status")), [])

	def test_confirmation_aborts_when_audit_cannot_be_saved(self):
		doc = MagicMock(); doc.name = "SO-1"; doc.company = "MX"; doc.docstatus = 1
		doc.get.side_effect = self.doc.get
		with patch.object(release, "read_order", return_value=doc), patch.object(release, "review_order", return_value={"can_release": True, "receipts": [], "receipts_complete": True}), patch.object(frappe.db, "get_value"), patch.object(flow, "is_crm_integration_enabled", return_value=True), patch.object(flow, "validate_mes_sync_available"), patch.object(flow, "create_crm_log", return_value=None), patch.object(flow, "set_process_status") as status, patch.object(flow, "enqueue_confirm_deposit_and_push_to_mes") as enqueue:
			with self.assertRaises(frappe.ValidationError): flow.confirm_deposit_and_push_to_mes("SO-1")
		status.assert_not_called(); enqueue.assert_not_called()

	def test_processing_confirmation_does_not_allow_production(self):
		with patch.object(release, "read_order", return_value=frappe._dict({**self.doc, "custom_process_status": release.PROCESSING})):
			with self.assertRaises(frappe.ValidationError): release.assert_production_released("SO-1")

	def test_production_cannot_link_an_order_from_another_company(self):
		with patch.object(release, "read_order", return_value=frappe._dict({**self.doc, "custom_process_status": "Pending Production"})):
			with self.assertRaises(frappe.ValidationError): release.assert_production_released("SO-1", "OTHER")

	def test_combined_production_plan_references_all_pass_the_same_gate(self):
		plan = frappe._dict(doctype="Production Plan", company="MX", po_items=[frappe._dict(sales_order="SO-1")], prod_plan_references=[frappe._dict(sales_order="SO-2"), frappe._dict(sales_order="SO-1")])
		with patch.object(release, "assert_production_released") as gate:
			release.validate_production_sources(plan)
		self.assertEqual(gate.call_count, 2)
		gate.assert_any_call("SO-1", "MX"); gate.assert_any_call("SO-2", "MX")

	def test_existing_released_orders_continue_without_backfilling_audit(self):
		with patch.object(release, "read_order", return_value=frappe._dict({**self.doc, "custom_process_status": "Pending Production"})):
			release.assert_production_released("SO-1", "MX")

	def test_input_is_bounded_and_deduplicated(self):
		self.assertEqual(release.order_names(["SO-1", "SO-1"]), ["SO-1"])
		for invalid in ([], "{}", [1], ["SO"] * 101):
			with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError): release.order_names(invalid)

	def test_final_payment_cannot_bypass_pending_production_authorization(self):
		doc = MagicMock(); doc.name = "SO-1"; doc.company = "MX"; doc.docstatus = 1; doc.get.side_effect = self.doc.get
		with patch.object(frappe, "get_doc", return_value=doc), patch.object(release, "read_order", return_value=self.doc), patch.object(flow, "is_crm_integration_enabled", return_value=True), patch.object(flow, "set_process_status") as set_status:
			with self.assertRaises(frappe.ValidationError): flow.reconcile_final_payment("SO-1")
		set_status.assert_not_called()

	def test_native_document_writes_cannot_invent_a_released_status(self):
		doc = MagicMock(); doc.get.side_effect = lambda field: "MX" if field == "company" else "Pending Production"
		doc.get_doc_before_save.return_value = self.doc
		with self.assertRaises(frappe.ValidationError): release.protect_process_status(doc)

	def test_native_edits_preserve_existing_status_and_new_orders_start_pending(self):
		doc = MagicMock(); doc.get.side_effect = self.doc.get; doc.get_doc_before_save.return_value = self.doc
		release.protect_process_status(doc)
		doc.get.side_effect = lambda field: "MX" if field == "company" else "Pending Confirmation"
		doc.get_doc_before_save.return_value = None
		release.protect_process_status(doc)

	def test_hidden_wrong_company_receipt_blocks_without_disclosing_its_identity(self):
		payment = self.payment(); payment.company = "OTHER"
		with patch.object(frappe, "get_all", return_value=["HIDDEN"]), patch.object(frappe, "get_doc", return_value=payment), patch.object(frappe, "has_permission", return_value=False):
			review = release.review_order(self.doc)
		self.assertFalse(review["can_release"])
		self.assertNotIn("HIDDEN", str(review)); self.assertNotIn("OTHER", str(review))
