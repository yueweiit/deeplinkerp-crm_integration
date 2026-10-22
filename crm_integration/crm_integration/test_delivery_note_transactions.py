"""Shipment failure cleanup without a site or business database writes."""

from contextlib import ExitStack, contextmanager, nullcontext
from importlib.util import find_spec
from types import MethodType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.stock_ledger import NegativeStockError

from crm_integration.crm_integration import delivery_note as shipment


@contextmanager
def request_local(**values):
	# Frappe Local has no __dict__; mock.patch(create=True) deletes old values
	# on exit instead of restoring them. Preserve the enclosing test site.
	missing = object()
	previous = {key: getattr(frappe.local, key, missing) for key in values}
	try:
		for key, value in values.items():
			setattr(frappe.local, key, value)
		yield
	finally:
		for key, value in previous.items():
			if value is missing:
				if hasattr(frappe.local, key):
					delattr(frappe.local, key)
			else:
				setattr(frappe.local, key, value)


class TestCRMShipmentTransactions(TestCase):
	def setUp(self):
		self.pending_writes = []
		self.savepoints = set()
		self.after_commit = []
		self.db = MagicMock()
		self.db.savepoint.side_effect = self.savepoints.add
		self.db.rollback.side_effect = self.rollback
		self.order = frappe._dict(name="SO-1", company="Company")
		self.doc = MagicMock(name="delivery_note")
		self.doc.name = "DN-1"
		self.payload = dict(
			custom_crm_shipment_no="SHIP-1", sales_order="SO-1",
			posting_date="2026-09-10", posting_time="02:18:24",
			items=[dict(item_code="ITEM-1", qty=10, warehouse="FG-WH")],
		)
		self.response = dict(name="DN-1", docstatus=1, stock_updated=True)
		self.stack = self.enterContext(ExitStack())
		self.stack.enter_context(patch.object(frappe, "db", self.db))
		self.stack.enter_context(request_local(db=self.db, response=frappe._dict()))
		self.logger = self.stack.enter_context(patch.object(frappe, "logger")).return_value
		self.stack.enter_context(patch.object(frappe, "get_doc", return_value=self.order))
		self.stack.enter_context(patch.object(shipment, "now", return_value="2026-10-22 12:00:00"))
		self.stack.enter_context(patch.object(shipment, "get_crm_frozen_committed_receipt", return_value=None))
		self.stack.enter_context(patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order))
		self.stack.enter_context(patch.object(shipment, "is_crm_integration_enabled", return_value=True))
		self.stack.enter_context(patch.object(shipment, "create_crm_log"))
		self.log_update = self.stack.enter_context(patch.object(shipment, "update_crm_log"))
		self.stack.enter_context(patch.object(shipment, "crm_shipment_lock", side_effect=lambda *a: nullcontext()))
		self.stack.enter_context(patch.object(shipment, "crm_stock_pool_locks", side_effect=lambda *a: nullcontext()))
		self.stack.enter_context(patch.object(shipment, "lock_sales_order_for_crm_shipment"))
		self.stack.enter_context(patch.object(shipment, "get_crm_delivery_note_for_allocation_credit", return_value=None))
		self.stack.enter_context(patch.object(shipment, "allocate_crm_shipment_items", return_value=[]))
		self.stack.enter_context(patch.object(shipment, "build_crm_stock_plan", return_value={"blockers": []}))
		self.preview = self.stack.enter_context(patch.object(shipment, "get_crm_preview_response", return_value=dict(
			source_fingerprint="source", preview_fingerprint="preview")))
		self.stack.enter_context(patch.object(shipment, "release_sales_order_for_crm_shipment",
			side_effect=lambda *a, **k: self.write("order_status")))
		self.stack.enter_context(patch.object(shipment, "persist_crm_order_item_mappings",
			side_effect=lambda *a: self.write("row_mapping")))
		self.stack.enter_context(patch.object(shipment, "get_or_create_crm_delivery_note",
			side_effect=self.create_delivery))
		self.stack.enter_context(patch.object(shipment, "make_crm_stock_supplement", side_effect=self.create_supplement))
		self.prepare = self.stack.enter_context(patch.object(shipment, "prepare_crm_delivery_note_for_submit"))
		self.record_reposts = self.stack.enter_context(patch.object(shipment, "record_crm_repost_dependencies"))
		self.get_response = self.stack.enter_context(patch.object(shipment, "get_crm_shipment_response", return_value=self.response))

	def write(self, name):
		self.pending_writes.append(name)
		self.after_commit.append(name)

	def create_delivery(self, *args):
		self.write("delivery_note")
		return self.doc, False

	def create_supplement(self, *args):
		self.write("stock_supplement")
		return frappe._dict(name="STE-1")

	def rollback(self, *, save_point=None):
		if save_point:
			if save_point not in self.savepoints:
				raise RuntimeError("SAVEPOINT does not exist")
		else:
			self.savepoints.clear()
			self.after_commit.clear()
		self.pending_writes.clear()

	def assert_failed_cleanly(self, error):
		with self.assertRaises(type(error)) as raised:
			shipment.create_and_submit_delivery_note_from_crm(self.payload)
		self.assertIs(raised.exception, error)
		self.assertEqual(self.pending_writes, [])
		self.assertEqual(self.after_commit, [])
		self.db.savepoint.assert_not_called()
		self.assertTrue(all(not call.args and not call.kwargs for call in self.db.rollback.call_args_list))
		self.db.commit.assert_not_called()
		self.get_response.assert_not_called()
		self.assertEqual(self.log_update.call_args.kwargs["status"], "Failed")
		self.assertIn(str(error), self.log_update.call_args.kwargs["error_message"])

	def test_legacy_shortage_after_inner_full_rollback_preserves_native_error(self):
		error = NegativeStockError("10 units needed at historical posting time")

		def submit():
			self.write("stock_ledger")
			self.db.rollback()  # Controller protection boundary already aborted.
			raise error

		self.doc.submit.side_effect = submit
		self.assert_failed_cleanly(error)
		self.assertEqual(error.http_status_code, 417)
		self.assertEqual(self.db.rollback.call_count, 2)

	def test_failure_without_controller_boundary_clears_all_pending_work(self):
		error = NegativeStockError("insufficient historical stock")
		self.doc.submit.side_effect = error
		self.assert_failed_cleanly(error)
		self.db.rollback.assert_called_once_with()

	def test_installed_purchase_boundary_and_shipment_cleanup_preserve_original_error(self):
		if find_spec("deeplinkerp_branding") is None:
			self.skipTest("Optional procurement boundary app is not available")
		from deeplinkerp_branding.services import purchase_repost_boundary as boundary

		error = NegativeStockError("native historical shortage inside controller boundary")
		state = SimpleNamespace(depth=0, poisoned=False, db=self.db)

		def submit():
			with boundary.execution(db=self.db):
				self.write("stock_ledger")
				raise error

		self.doc.submit.side_effect = submit
		with patch.object(boundary, "initialize", return_value=state):
			self.assert_failed_cleanly(error)
		self.assertEqual(state.depth, 0)
		self.assertEqual(self.db.rollback.call_count, 2)

	def test_deadlock_that_invalidated_savepoints_preserves_original_error(self):
		error = frappe.QueryDeadlockError("transaction was rolled back by database")

		def submit():
			self.pending_writes.clear()
			self.savepoints.clear()  # InnoDB aborts independently of Python callbacks.
			raise error

		self.doc.submit.side_effect = submit
		self.assert_failed_cleanly(error)
		self.db.rollback.assert_called_once_with()

	def test_failure_before_submit_also_rolls_back_order_and_draft(self):
		error = frappe.ValidationError("invalid batch or serial number")
		self.prepare.side_effect = error
		self.assert_failed_cleanly(error)
		self.doc.submit.assert_not_called()

	def test_strict_shipment_failure_rolls_back_supplement_too(self):
		self.payload.update(
			source_organization="ORG", event_id="EVENT", allow_stock_supplement=True,
			source_fingerprint="source", preview_fingerprint="preview",
		)
		self.payload["items"][0].update(crm_order_item_id="ROW-1", custom_version="", uom="Nos")
		self.payload["source_fingerprint"] = shipment.get_crm_shipment_source_fingerprint(
			shipment.normalize_crm_shipment_payload(self.payload)
		)
		self.preview.return_value["source_fingerprint"] = self.payload["source_fingerprint"]
		error = frappe.ValidationError("native accounting period closed")
		observed = []

		def submit():
			observed.extend(self.pending_writes)
			raise error

		self.doc.submit.side_effect = submit
		self.assert_failed_cleanly(error)
		self.assertIn("stock_supplement", observed)

	def test_success_preserves_writes_and_callbacks_for_request_commit(self):
		result = shipment.create_and_submit_delivery_note_from_crm(self.payload)
		self.assertIs(result, self.response)
		self.doc.submit.assert_called_once_with()
		self.record_reposts.assert_called_once()
		self.assertIn("delivery_note", self.pending_writes)
		self.assertTrue(self.after_commit)
		self.db.rollback.assert_not_called()
		self.db.savepoint.assert_not_called()
		self.db.commit.assert_not_called()
		self.assertEqual(self.log_update.call_args.kwargs["status"], "Success")

	def assert_cleanup_failure_preserves_original(self, error):
		self.doc.submit.side_effect = error
		self.db.rollback.side_effect = RuntimeError("rollback callback failed: private diagnostic")
		with self.assertRaises(type(error)) as raised:
			shipment.create_and_submit_delivery_note_from_crm(self.payload)
		self.assertIs(raised.exception, error)
		self.assertIsNone(frappe.local.db)
		self.db.close.assert_called_once_with()
		self.db.rollback.assert_called_once_with()
		self.db.commit.assert_not_called()
		self.get_response.assert_not_called()
		self.assertIsNone(self.log_update.call_args.args[0])
		self.assertIn(str(error), self.log_update.call_args.kwargs["error_message"])

	def test_rollback_failure_closes_and_detaches_db_preserving_stock_error(self):
		error = NegativeStockError("historical stock shortage")
		self.assert_cleanup_failure_preserves_original(error)
		self.assertEqual(error.http_status_code, 417)
		self.logger.error.assert_called_once_with(
			"CRM shipment transaction cleanup failed: %s (%s)", "rollback_failed", "RuntimeError"
		)

	def test_rollback_failure_preserves_deadlock_for_framework_retry_semantics(self):
		self.assert_cleanup_failure_preserves_original(frappe.QueryDeadlockError("original deadlock"))

	def test_close_failure_does_not_replace_original_or_leave_request_db_attached(self):
		self.db.close.side_effect = OSError("connection close failed")
		self.assert_cleanup_failure_preserves_original(NegativeStockError("original shortage"))
		self.assertEqual([call.args[1] for call in self.logger.error.call_args_list],
			["close_failed", "rollback_failed"])

	def test_diagnostic_failure_does_not_prevent_detach_or_replace_original(self):
		self.logger.error.side_effect = OSError("diagnostic sink unavailable")
		self.assert_cleanup_failure_preserves_original(NegativeStockError("original shortage"))

	def test_close_and_diagnostic_failure_together_still_preserve_original(self):
		self.db.close.side_effect = OSError("connection close failed")
		self.logger.error.side_effect = OSError("diagnostic sink unavailable")
		self.assert_cleanup_failure_preserves_original(NegativeStockError("original shortage"))

	def test_framework_response_retains_stock_error_and_does_not_reuse_failed_db(self):
		from frappe.app import handle_exception

		error = NegativeStockError("historical stock shortage")
		self.doc.submit.side_effect = error
		self.db.rollback.side_effect = RuntimeError("rollback failed: private diagnostic")
		request = SimpleNamespace(path="/api/method/test", headers={"Accept": "application/json"})
		with ExitStack() as stack:
			stack.enter_context(patch.object(frappe, "db", frappe.local("db")))
			stack.enter_context(request_local(
				request=request, is_ajax=True, conf=frappe._dict(developer_mode=0),
				session=frappe._dict(user="crm-test"), flags=frappe._dict(),
				error_log=[], debug_log=[], message_log=[{"message": str(error)}],
			))
			try:
				shipment.create_and_submit_delivery_note_from_crm(self.payload)
			except NegativeStockError as caught:
				self.assertIs(caught, error)
				response = handle_exception(caught)
			else:
				self.fail("The shipment must propagate the original stock error")
			self.assertFalse(frappe.db)
			self.assertIsNone(getattr(frappe.local, "db", None))
		self.assertEqual(response.status_code, 417)
		self.assertEqual(response.get_json()["exc_type"], "NegativeStockError")
		self.assertIn(str(error), response.get_json()["_server_messages"])
		self.assertNotIn("private diagnostic", response.get_data(as_text=True))
		self.db.rollback.assert_called_once_with()

	def test_native_before_and_after_rollback_callback_failures_close_physical_owner(self):
		from frappe.database.database import Database
		from frappe.utils import CallbackManager

		for phase in ("before_rollback", "after_rollback"):
			with self.subTest(phase=phase):
				owner = MagicMock()
				db = SimpleNamespace(
					_conn=owner, _cursor=object(), _disable_transaction_control=False,
					value_cache={}, sql=MagicMock(), begin=MagicMock(),
					**{name: CallbackManager() for name in
						("before_commit", "after_commit", "before_rollback", "after_rollback")},
				)
				db.rollback = MagicMock(wraps=MethodType(Database.rollback, db))
				db.close = MagicMock(wraps=MethodType(Database.close, db))
				notification = MagicMock()
				db.after_commit.add(notification)
				getattr(db, phase).add(MagicMock(side_effect=RuntimeError("native rollback callback failed")))
				error = NegativeStockError("original stock shortage")
				self.doc.submit.side_effect = error
				with patch.object(frappe, "db", db), request_local(db=db):
					with self.assertRaises(NegativeStockError) as raised:
						shipment.create_and_submit_delivery_note_from_crm(self.payload)
					self.assertIs(raised.exception, error)
					self.assertIsNone(frappe.local.db)
				owner.close.assert_called_once_with()
				self.assertIsNone(db._conn)
				self.assertIsNone(db._cursor)
				notification.assert_not_called()
				db.after_commit.run()
				notification.assert_not_called()
				if phase == "before_rollback":
					db.sql.assert_not_called()
				else:
					db.sql.assert_called_once_with("rollback")
