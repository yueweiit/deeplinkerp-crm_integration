import builtins
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase
from requests.exceptions import ConnectionError as RequestsConnectionError

from crm_integration.crm_integration import sales_order as crm_sales_order
from crm_integration.crm_integration import settings as crm_settings
from crm_integration.crm_integration.sales_order import (
	CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION,
	CRM_STATUS_IN_PRODUCTION,
	CRM_STATUS_PRODUCTION_PROGRESS_REPORTED,
	DEPOSIT_CONFIRMATION_PROCESSING,
	PENDING_PRODUCTION,
	build_mes_sales_order_payload,
	confirm_deposit_and_push_to_mes,
	confirm_deposit_and_push_to_mes_job,
	enqueue_confirm_deposit_and_push_to_mes,
	get_mes_integration_services,
	make_crm_trace_id,
	prevent_deposit_confirmation_processing_cancel,
	prevent_duplicate_crm_order_no,
	push_sales_order_to_mes,
	push_sales_order_status_payload_to_crm,
	reconcile_final_payment,
	reject_sales_order,
	restore_deposit_confirmation_after_failure,
	run_confirm_deposit_sync,
)


class TestCRMShipmentStatusPushSwitch(UnitTestCase):
	def test_site_config_defaults_to_enabled_and_allows_opt_out(self):
		for config, expected in (
			({}, True),
			({"crm_shipment_status_push_enabled": 0}, False),
			({"crm_shipment_status_push_enabled": 1}, True),
		):
			with self.subTest(config=config), patch.object(frappe.local, "conf", frappe._dict(config)):
				self.assertEqual(crm_settings.is_crm_shipment_status_push_enabled(), expected)

	def test_paused_shipment_events_are_not_enqueued(self):
		with (
			patch.object(crm_sales_order, "is_crm_shipment_status_push_enabled", return_value=False),
			patch.object(frappe, "enqueue") as enqueue,
		):
			for status in (
				crm_sales_order.CRM_STATUS_SHIPMENT_CREATED,
				crm_sales_order.CRM_STATUS_SHIPMENT_CANCELLED,
			):
				crm_sales_order.enqueue_sales_order_status_to_crm("SO-001", status)
			enqueue.assert_not_called()
			crm_sales_order.enqueue_sales_order_status_to_crm(
				"SO-001", crm_sales_order.CRM_STATUS_PENDING_SHIPMENT
			)
			enqueue.assert_called_once()

	def test_enabled_shipment_event_is_enqueued(self):
		with (
			patch.object(crm_sales_order, "is_crm_shipment_status_push_enabled", return_value=True),
			patch.object(frappe, "enqueue") as enqueue,
		):
			crm_sales_order.enqueue_sales_order_status_to_crm(
				"SO-001", crm_sales_order.CRM_STATUS_SHIPMENT_CREATED
			)
			enqueue.assert_called_once()

	def test_queued_shipment_event_is_discarded_when_paused(self):
		with (
			patch.object(frappe, "get_doc") as get_doc,
			patch.object(crm_sales_order, "is_crm_shipment_status_push_enabled", return_value=False),
			patch.object(crm_sales_order, "create_crm_log") as create_log,
			patch.object(crm_sales_order.requests, "post") as post,
		):
			for status in (
				crm_sales_order.CRM_STATUS_SHIPMENT_CREATED,
				crm_sales_order.CRM_STATUS_SHIPMENT_CANCELLED,
			):
				result = crm_sales_order.push_sales_order_status_to_crm_job("SO-001", status)
				self.assertIsNone(result)
		get_doc.assert_not_called()
		create_log.assert_not_called()
		post.assert_not_called()


class TestMESSalesOrderPayload(UnitTestCase):
	def test_connection_failure_records_failed_mes_log_without_http_response(self):
		order = frappe._dict(name="SO-001")
		log = frappe._dict(name="MES-LOG-001")
		services = frappe._dict(create_log=MagicMock(return_value=log), update_log=MagicMock())

		with (
			patch("crm_integration.crm_integration.sales_order.validate_mes_sync_available", return_value=services),
			patch("crm_integration.crm_integration.sales_order.build_mes_sales_order_payload", return_value={"doc_name": order.name}),
			patch("crm_integration.crm_integration.sales_order.get_mes_sales_order_push_url", return_value="http://mes.example/sales-order"),
			patch("crm_integration.crm_integration.sales_order.get_mes_sales_order_push_authorization", return_value="test-authorization"),
			patch("crm_integration.crm_integration.sales_order.requests.post", side_effect=RequestsConnectionError("connection refused")),
			patch.object(frappe.db, "commit") as commit,
			patch.object(frappe, "log_error"),
			self.assertRaisesRegex(frappe.ValidationError, "MES 销售订单推送失败"),
		):
			push_sales_order_to_mes(order)

		services.update_log.assert_called_once()
		self.assertEqual(services.update_log.call_args.args, (log,))
		self.assertEqual(services.update_log.call_args.kwargs["status"], "Failed")
		self.assertEqual(services.update_log.call_args.kwargs["http_status_code"], 0)
		commit.assert_called_once_with()

	def test_versions_are_preserved_per_sales_order_line(self):
		order = frappe._dict(
			name="SO-001",
			custom_crm_order_no="CRM-001",
			items=[
				frappe._dict(name="SOI-PRINT", item_code="ITEM-001", qty=100, custom_version="PC_PRINT"),
				frappe._dict(name="SOI-OTHER", item_code="ITEM-001", qty=50, custom_version="OTHER_VERSION"),
			],
		)

		payload = build_mes_sales_order_payload(order)
		self.assertEqual(
			[(row["name"], row["custom_version"]) for row in payload["data"]["items"]],
			[("SOI-PRINT", "PC_PRINT"), ("SOI-OTHER", "OTHER_VERSION")],
		)

	def test_orders_without_versions_keep_optional_field_absent(self):
		for version_fields in ({}, {"custom_version": None}, {"custom_version": ""}):
			with self.subTest(version_fields=version_fields):
				order = frappe._dict(
					name="SO-001",
					custom_crm_order_no="CRM-001",
					items=[frappe._dict(item_code="ITEM-001", qty=100, **version_fields)],
				)

				payload = build_mes_sales_order_payload(order)
				self.assertEqual(len(payload["data"]["items"]), 1)
				self.assertNotIn("custom_version", payload["data"]["items"][0])

	def test_production_progress_payload_includes_erp_delivery_note_number(self):
		sales_order = frappe._dict(
			name="SO-001",
			custom_crm_order_no="CRM-001",
			custom_process_status="Pending Final Payment",
		)
		response = MagicMock(status_code=200, headers={"content-type": "application/json"})
		response.json.return_value = {"code": 200}

		with (
			patch("crm_integration.crm_integration.sales_order.create_crm_log"),
			patch("crm_integration.crm_integration.sales_order.update_crm_log"),
			patch(
				"crm_integration.crm_integration.sales_order.get_crm_status_api_url",
				return_value="https://crm.example/status",
			),
			patch(
				"crm_integration.crm_integration.sales_order.get_crm_status_api_key",
				return_value="test-key",
			),
			patch(
				"crm_integration.crm_integration.sales_order.get_crm_status_api_verify_ssl",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.requests.post",
				return_value=response,
			) as post,
		):
			push_sales_order_status_payload_to_crm(
				sales_order=sales_order,
				external_status=CRM_STATUS_PRODUCTION_PROGRESS_REPORTED,
				delivery_note_name="MAT-DN-2026-00001",
				production_batch_no="BATCH-001",
			)

		payload = post.call_args.kwargs["json"]
		self.assertEqual(payload["deliveryNoteName"], "MAT-DN-2026-00001")
		self.assertEqual(payload["delivery_note"], "MAT-DN-2026-00001")


class TestSalesOrderPermissions(UnitTestCase):
	def setUp(self):
		super().setUp()
		def scoped_read(name):
			doc = frappe.get_doc("Sales Order", name)
			doc.check_permission("read")
			return doc
		self.enterContext(patch("crm_integration.crm_integration.finance_release.read_order", side_effect=scoped_read))
		self.enterContext(patch("crm_integration.crm_integration.finance_release.has_release_permission", return_value=True))
		self.enterContext(patch("crm_integration.crm_integration.finance_release.review_order", return_value={"can_release": True, "receipts": [], "receipts_complete": True}))
		self.enterContext(patch.object(frappe.db, "get_value", return_value="SO-001"))

	def test_duplicate_crm_order_no_is_rejected_before_insert(self):
		doc = frappe._dict(custom_crm_order_no="CRM-001")

		with (
			patch(
				"crm_integration.crm_integration.sales_order.get_sales_order_names_by_crm_order_no",
				return_value=["SO-EXISTING"],
			),
			self.assertRaises(frappe.ValidationError),
		):
			prevent_duplicate_crm_order_no(doc)

	def test_reject_checks_cancel_permission_before_crm_push(self):
		doc = self.make_sales_order()
		doc.check_permission.side_effect = frappe.PermissionError

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm"
			) as push_status,
			self.assertRaises(frappe.PermissionError),
		):
			reject_sales_order("SO-001")

		doc.check_permission.assert_called_once_with("cancel")
		push_status.assert_not_called()

	def test_confirm_deposit_checks_read_scope_before_push(self):
		doc = self.make_sales_order()
		doc.check_permission.side_effect = frappe.PermissionError

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm"
			) as push_status,
			self.assertRaises(frappe.PermissionError),
		):
			confirm_deposit_and_push_to_mes("SO-001")

		doc.check_permission.assert_called_once_with("read")
		push_status.assert_not_called()

	def test_order_cannot_be_cancelled_while_deposit_confirmation_is_processing(self):
		doc = self.make_sales_order(DEPOSIT_CONFIRMATION_PROCESSING)

		with (
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			self.assertRaisesRegex(frappe.ValidationError, "暂时不能取消销售订单"),
		):
			prevent_deposit_confirmation_processing_cancel(doc)

	def test_confirm_deposit_queues_external_sync_after_commit(self):
		doc = self.make_sales_order()

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch("crm_integration.crm_integration.sales_order.create_crm_log", return_value=frappe._dict(name="AUDIT-1")),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.enqueue_confirm_deposit_and_push_to_mes"
			) as enqueue_sync,
			patch("crm_integration.crm_integration.sales_order.validate_mes_sync_available"),
			patch("crm_integration.crm_integration.sales_order.set_process_status") as set_status,
			patch(
				"crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm"
			) as push_status,
			patch("crm_integration.crm_integration.sales_order.push_sales_order_to_mes") as push_mes,
		):
			result = confirm_deposit_and_push_to_mes("SO-001")

		enqueue_sync.assert_called_once_with("SO-001", "AUDIT-1")
		set_status.assert_called_once_with(doc, DEPOSIT_CONFIRMATION_PROCESSING)
		push_status.assert_not_called()
		push_mes.assert_not_called()
		self.assertTrue(result["queued"])
		self.assertEqual(result["process_status"], DEPOSIT_CONFIRMATION_PROCESSING)

	def test_confirm_deposit_does_not_enqueue_again_while_processing(self):
		doc = self.make_sales_order(DEPOSIT_CONFIRMATION_PROCESSING)

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.enqueue_confirm_deposit_and_push_to_mes"
			) as enqueue_sync,
		):
			result = confirm_deposit_and_push_to_mes("SO-001")

		enqueue_sync.assert_not_called()
		self.assertTrue(result["queued"])
		self.assertTrue(result["idempotent_replay"])
		self.assertEqual(result["process_status"], DEPOSIT_CONFIRMATION_PROCESSING)

	def test_missing_mes_app_returns_clear_validation_error(self):
		original_import = builtins.__import__

		def import_without_mes(name, *args, **kwargs):
			if name.startswith("mes_integration"):
				error = ModuleNotFoundError("No module named 'mes_integration'")
				error.name = "mes_integration"
				raise error
			return original_import(name, *args, **kwargs)

		with (
			patch("builtins.__import__", side_effect=import_without_mes),
			self.assertRaisesRegex(frappe.ValidationError, "mes_integration"),
		):
			get_mes_integration_services()

	def test_confirm_deposit_job_is_deduplicated(self):
		with patch.object(frappe, "enqueue") as enqueue:
			enqueue_confirm_deposit_and_push_to_mes("SO-001")

		enqueue.assert_called_once_with(
			"crm_integration.crm_integration.sales_order.confirm_deposit_and_push_to_mes_job",
			queue="short",
			enqueue_after_commit=True,
			job_id="crm-confirm-deposit-SO-001",
			deduplicate=True,
			sales_order_name="SO-001",
			audit_log=None,
		)

	def test_confirm_deposit_job_advances_status_only_after_external_sync(self):
		doc = self.make_sales_order(DEPOSIT_CONFIRMATION_PROCESSING)
		operations = MagicMock()

		with (
			patch(
				"crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm"
			) as push_status,
			patch("crm_integration.crm_integration.sales_order.push_sales_order_to_mes") as push_mes,
			patch("crm_integration.crm_integration.sales_order.set_process_status") as set_status,
		):
			operations.attach_mock(push_status, "push_status")
			operations.attach_mock(push_mes, "push_mes")
			operations.attach_mock(set_status, "set_status")
			run_confirm_deposit_sync(doc)

		self.assertEqual(
			operations.mock_calls,
			[
				call.push_status(doc, CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION),
				call.push_status(doc, CRM_STATUS_IN_PRODUCTION),
				call.push_mes(doc),
				call.set_status(doc, PENDING_PRODUCTION),
			],
		)

	def test_failed_confirm_deposit_job_persists_failure_log(self):
		doc = self.make_sales_order(DEPOSIT_CONFIRMATION_PROCESSING)

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.sales_order.run_confirm_deposit_sync",
				side_effect=RuntimeError("MES unavailable"),
			),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe.db, "commit") as commit,
			patch(
				"crm_integration.crm_integration.sales_order.restore_deposit_confirmation_after_failure"
			) as restore_status,
			patch("crm_integration.crm_integration.sales_order.create_crm_log") as create_log,
			self.assertRaisesRegex(RuntimeError, "MES unavailable"),
		):
			confirm_deposit_and_push_to_mes_job("SO-001")

		rollback.assert_called_once_with()
		restore_status.assert_called_once_with("SO-001")
		create_log.assert_called_once()
		commit.assert_called_once_with()

	def test_mes_failure_preserves_confirmed_crm_production_milestone(self):
		doc = frappe._dict(
			name="SO-001",
			docstatus=1,
			status="To Deliver and Bill",
			custom_process_status=DEPOSIT_CONFIRMATION_PROCESSING,
			custom_crm_production_started=0,
			custom_crm_production_milestone="NOT_STARTED",
		)

		def db_set(field, value=None, **kwargs):
			if isinstance(field, dict):
				doc.update(field)
			else:
				doc[field] = value

		doc.db_set = MagicMock(side_effect=db_set)
		with (
			patch("crm_integration.crm_integration.sales_order.push_sales_order_status_to_crm") as push_crm,
			patch("crm_integration.crm_integration.sales_order.push_sales_order_to_mes", side_effect=RuntimeError("MES 401")),
			patch("crm_integration.crm_integration.sales_order.set_process_status") as set_status,
			patch.object(frappe.db, "commit") as commit,
			self.assertRaisesRegex(RuntimeError, "MES 401"),
		):
			run_confirm_deposit_sync(doc)

		self.assertEqual(push_crm.call_args_list, [
			call(doc, CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION),
			call(doc, CRM_STATUS_IN_PRODUCTION),
		])
		self.assertEqual(doc.custom_crm_production_milestone, "REACHED")
		self.assertEqual(doc.custom_crm_production_started, 1)
		self.assertEqual(doc.custom_process_status, DEPOSIT_CONFIRMATION_PROCESSING)
		self.assertEqual(commit.call_count, 2)
		set_status.assert_not_called()

	def test_failed_confirm_deposit_restores_retryable_status(self):
		doc = self.make_sales_order(DEPOSIT_CONFIRMATION_PROCESSING)

		with patch.object(frappe, "get_doc", return_value=doc):
			restore_deposit_confirmation_after_failure("SO-001")

		doc.db_set.assert_called_once_with(
			"custom_process_status",
			"Pending Deposit Confirmation",
			update_modified=True,
		)
		doc.notify_update.assert_called_once_with()

	def test_crm_status_trace_id_is_stable_for_retries(self):
		first = make_crm_trace_id("SO-001", CRM_STATUS_IN_PRODUCTION)
		second = make_crm_trace_id("SO-001", CRM_STATUS_IN_PRODUCTION)

		self.assertEqual(first, second)

	def test_reconcile_final_payment_checks_write_permission_before_status_change(self):
		doc = self.make_sales_order()
		doc.check_permission.side_effect = frappe.PermissionError

		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.sales_order.is_crm_integration_enabled",
				return_value=True,
			),
			patch("crm_integration.crm_integration.sales_order.set_process_status") as set_status,
			self.assertRaises(frappe.PermissionError),
		):
			reconcile_final_payment("SO-001")

		doc.check_permission.assert_called_once_with("write")
		set_status.assert_not_called()

	def make_sales_order(self, process_status="Pending Deposit Confirmation"):
		doc = MagicMock()
		doc.name = "SO-001"
		doc.docstatus = 1
		doc.get.side_effect = lambda field: {
			"company": "Test Company",
			"custom_process_status": process_status,
			"status": "To Deliver and Bill",
		}.get(field)
		return doc
