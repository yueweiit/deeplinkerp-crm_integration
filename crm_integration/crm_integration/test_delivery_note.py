import builtins
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import delivery_note as shipment
from crm_integration.crm_integration.delivery_note import (
	allocate_crm_shipment_items,
	apply_sales_order_item_pricing,
	delivery_note_dimensions_match,
	enqueue_crm_delivery_note_cancellation_event,
	enqueue_mes_delivery_note_status_callback,
	get_or_create_crm_delivery_note,
	update_sales_orders_delivery_process_status,
	validate_crm_shipment_payload,
)
from crm_integration.crm_integration.sales_order import CRM_STATUS_SHIPMENT_CANCELLED


class TestCRMShipmentAllocation(UnitTestCase):
	def test_crm_number_selects_effective_order_after_resubmission(self):
		order = frappe._dict(name="SO-NEW", docstatus=1, custom_crm_order_no="CRM-001")
		with (
			patch.object(shipment, "get_sales_order_names_by_crm_order_no", return_value=["SO-NEW"]) as names,
			patch.object(frappe, "get_doc", return_value=order) as get_doc,
		):
			result = shipment.get_crm_shipment_sales_order({"custom_crm_order_no": "CRM-001"})
		names.assert_called_once_with("CRM-001", docstatus=1)
		get_doc.assert_called_once_with("Sales Order", "SO-NEW")
		self.assertIs(result, order)

	def test_crm_number_rejects_multiple_effective_orders(self):
		with (
			patch.object(shipment, "get_sales_order_names_by_crm_order_no", return_value=["SO-A", "SO-B"]),
			patch.object(frappe, "get_doc") as get_doc,
		):
			with self.assertRaises(shipment.CRMShipmentIdentityConflict):
				shipment.get_crm_shipment_sales_order({"custom_crm_order_no": "CRM-001"})
		get_doc.assert_not_called()

	def test_zero_sales_order_rate_is_not_replaced_by_item_price(self):
		source = frappe._dict(
			price_list_rate=0,
			base_price_list_rate=0,
			discount_percentage=0,
			discount_amount=0,
			rate=0,
			base_rate=0,
		)
		target = frappe._dict(qty=50, price_list_rate=23.5, rate=23.5, amount=1175)
		target.set = lambda fieldname, value: target.__setitem__(fieldname, value)

		apply_sales_order_item_pricing(target, source)

		self.assertEqual(target.price_list_rate, 0)
		self.assertEqual(target.rate, 0)
		self.assertEqual(target.amount, 0)
		self.assertEqual(target.base_amount, 0)

	def test_nonzero_sales_order_rate_is_scaled_by_shipment_quantity(self):
		source = frappe._dict(
			price_list_rate=20,
			base_price_list_rate=7.6,
			discount_percentage=12.5,
			discount_amount=2.5,
			rate=17.5,
			base_rate=6.65,
		)
		target = frappe._dict(qty=4)
		target.set = lambda fieldname, value: target.__setitem__(fieldname, value)

		apply_sales_order_item_pricing(target, source)

		self.assertEqual(target.price_list_rate, 20)
		self.assertEqual(target.rate, 17.5)
		self.assertEqual(target.amount, 70)
		self.assertEqual(target.base_amount, 26.6)

	def test_crm_shipment_requires_stable_shipment_number(self):
		with self.assertRaisesRegex(frappe.ValidationError, "custom_crm_shipment_no"):
			validate_crm_shipment_payload(
				{
					"sales_order": "SO-001",
					"items": [{"item_code": "ITEM-001", "qty": 1}],
				}
			)

	def test_crm_shipment_rejects_legacy_delivery_note_draft(self):
		with self.assertRaisesRegex(frappe.ValidationError, "delivery_note 参数已停用"):
			validate_crm_shipment_payload(
				{
					"custom_crm_shipment_no": "CRM-SHIP-001",
					"sales_order": "SO-001",
					"delivery_note": "DN-DRAFT-001",
					"items": [{"item_code": "ITEM-001", "qty": 1}],
				}
			)

	def test_submitted_delivery_note_quantity_is_credited_for_idempotent_replay(self):
		sales_order = frappe._dict(
			name="SO-001",
			docstatus=1,
			items=[
				frappe._dict(
					name="SOI-001",
					item_code="ITEM-001",
					qty=10,
					delivered_qty=10,
					custom_version="PC_PRINT",
				)
			],
		)
		delivery_note = frappe._dict(
			docstatus=1,
			items=[
				frappe._dict(
					against_sales_order="SO-001",
					so_detail="SOI-001",
					qty=4,
				)
			],
		)

		allocations = allocate_crm_shipment_items(
			sales_order,
			[
				{
					"sales_order_item": "SOI-001",
					"item_code": "ITEM-001",
					"custom_version": "PC_PRINT",
					"qty": 4,
				}
			],
			credit_delivery_note=delivery_note,
		)

		self.assertEqual(allocations[0].qty, 4)

	def test_version_selects_the_matching_sales_order_item(self):
		sales_order = frappe._dict(
			name="SO-001",
			docstatus=1,
			items=[
				frappe._dict(
					name="SOI-PRINT",
					item_code="ITEM-001",
					qty=5,
					delivered_qty=0,
					custom_version="PC_PRINT",
				),
				frappe._dict(
					name="SOI-OTHER",
					item_code="ITEM-001",
					qty=5,
					delivered_qty=0,
					custom_version="OTHER",
				),
			],
		)

		allocations = allocate_crm_shipment_items(
			sales_order,
			[{"item_code": "ITEM-001", "custom_version": "PC_PRINT", "qty": 2}],
		)
		self.assertEqual(allocations[0].sales_order_item.name, "SOI-PRINT")

		with self.assertRaisesRegex(frappe.ValidationError, "多个版本"):
			allocate_crm_shipment_items(sales_order, [{"item_code": "ITEM-001", "qty": 2}])

	def test_first_crm_shipment_creates_a_new_delivery_note(self):
		sales_order = frappe._dict(name="SO-001")
		payload = {"custom_crm_shipment_no": "CRM-SHIP-001"}
		allocations = [frappe._dict(qty=1)]
		created = frappe._dict(name="DN-001", docstatus=0)

		with (
			patch(
				"crm_integration.crm_integration.delivery_note.get_delivery_note_by_crm_shipment_no",
				return_value=None,
			),
			patch(
				"crm_integration.crm_integration.delivery_note.make_crm_delivery_note",
				return_value=created,
			) as make_delivery_note,
		):
			delivery_note, idempotent_replay = get_or_create_crm_delivery_note(
				sales_order, payload, allocations
			)

		self.assertIs(delivery_note, created)
		self.assertFalse(idempotent_replay)
		make_delivery_note.assert_called_once_with(sales_order, payload, allocations)

	def test_one_order_line_can_be_split_by_warehouse_and_batch(self):
		sales_order_item = frappe._dict(name="SOI-001")
		allocations = [
			frappe._dict(
				sales_order_item=sales_order_item,
				request_row=frappe._dict(
					warehouse="Warehouse A", batch_no="BATCH-A", serial_no="SERIAL-A"
				),
				qty=2,
			),
			frappe._dict(
				sales_order_item=sales_order_item,
				request_row=frappe._dict(warehouse="Warehouse B", batch_no="BATCH-B"),
				qty=3,
			),
		]
		delivery_note = frappe._dict(
			items=[
				frappe._dict(
					so_detail="SOI-001",
					warehouse="Warehouse A",
					batch_no="BATCH-A",
					serial_no="SERIAL-A",
					qty=2,
				),
				frappe._dict(
					so_detail="SOI-001", warehouse="Warehouse B", batch_no="BATCH-B", qty=3
				),
			]
		)

		self.assertTrue(delivery_note_dimensions_match(delivery_note, allocations))
		delivery_note["items"][1].warehouse = "Warehouse C"
		self.assertFalse(delivery_note_dimensions_match(delivery_note, allocations))


class TestDeliveryNoteCRMEvents(UnitTestCase):
	def test_missing_optional_mes_app_does_not_break_delivery_note(self):
		original_import = builtins.__import__

		def import_without_mes(name, *args, **kwargs):
			if name.startswith("mes_integration"):
				error = ModuleNotFoundError("No module named 'mes_integration'")
				error.name = "mes_integration"
				raise error
			return original_import(name, *args, **kwargs)

		with (
			patch("builtins.__import__", side_effect=import_without_mes),
			patch.object(frappe, "log_error") as log_error,
		):
			enqueue_mes_delivery_note_status_callback("DN-001")

		log_error.assert_not_called()

	def test_cancelled_delivery_note_enqueues_crm_reversal(self):
		doc = frappe._dict(
			name="DN-001",
			items=[
				frappe._dict(
					against_sales_order="SO-001",
					item_code="ITEM-001",
					stock_qty=2,
				)
			],
		)

		with patch(
			"crm_integration.crm_integration.delivery_note.enqueue_sales_order_status_to_crm"
		) as enqueue_status:
			enqueue_crm_delivery_note_cancellation_event(doc, "SO-001", "Deliverable")

		enqueue_status.assert_called_once_with(
			sales_order_name="SO-001",
			external_status=CRM_STATUS_SHIPMENT_CANCELLED,
			triggered_status="Deliverable",
			trigger_event="delivery_note_cancelled",
			delivery_note_name="DN-001",
			items=[{"externalItemId": "ITEM-001", "quantity": 2.0}],
			remark="销售出库 DN-001 已取消，ERP流程状态：Deliverable",
		)

	def test_cancel_path_enqueues_cancellation_instead_of_shipment(self):
		doc = frappe._dict(name="DN-001", company="Test Company", docstatus=2)
		sales_order = MagicMock(name="sales_order")

		with (
			patch(
				"crm_integration.crm_integration.delivery_note.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.delivery_note.get_linked_sales_orders",
				return_value=["SO-001"],
			),
			patch.object(frappe, "get_doc", return_value=sales_order),
			patch(
				"crm_integration.crm_integration.delivery_note.get_sales_order_delivery_process_status",
				return_value="Deliverable",
			),
			patch("crm_integration.crm_integration.delivery_note.set_process_status"),
			patch(
				"crm_integration.crm_integration.delivery_note.enqueue_crm_delivery_note_shipment_event"
			) as enqueue_shipment,
			patch(
				"crm_integration.crm_integration.delivery_note.enqueue_crm_delivery_note_cancellation_event"
			) as enqueue_cancellation,
			patch("crm_integration.crm_integration.delivery_note.enqueue_mes_delivery_note_status_callback"),
		):
			update_sales_orders_delivery_process_status(doc)

		enqueue_shipment.assert_not_called()
		enqueue_cancellation.assert_called_once_with(doc, "SO-001", "Deliverable")

	def test_crm_originated_submit_does_not_echo_shipment_to_crm(self):
		doc = frappe._dict(
			name="DN-001",
			company="Test Company",
			docstatus=1,
			custom_crm_shipment_no="CRM-SHIP-001",
		)
		sales_order = MagicMock(name="sales_order")

		with (
			patch(
				"crm_integration.crm_integration.delivery_note.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.delivery_note.get_linked_sales_orders",
				return_value=["SO-001"],
			),
			patch.object(frappe, "get_doc", return_value=sales_order),
			patch(
				"crm_integration.crm_integration.delivery_note.get_sales_order_delivery_process_status",
				return_value="Partially Delivered",
			),
			patch("crm_integration.crm_integration.delivery_note.set_process_status"),
			patch(
				"crm_integration.crm_integration.delivery_note.enqueue_crm_delivery_note_shipment_event"
			) as enqueue_shipment,
			patch(
				"crm_integration.crm_integration.delivery_note.enqueue_mes_delivery_note_status_callback"
			) as enqueue_mes,
		):
			update_sales_orders_delivery_process_status(doc)

		enqueue_shipment.assert_not_called()
		enqueue_mes.assert_called_once_with("DN-001")


class TestCRMNativeShipment(UnitTestCase):
	def warehouse_lookup(self, item_values=None, **values):
		get_value = frappe.db.get_value
		warehouse = frappe._dict(company="Company", disabled=0, is_group=0)
		warehouse.update(values)
		def lookup(doctype, *args, **kwargs):
			if doctype == "Warehouse":
				return warehouse
			if doctype == "Item":
				item = frappe._dict(disabled=0, is_stock_item=1, is_sales_item=1, has_variants=0)
				item.update(item_values or {})
				return item
			return get_value(doctype, *args, **kwargs)
		return lookup

	def source(self, **values):
		return dict(source_organization="CRM-YW", event_id="EVENT-1", batch_no="七月第一批",
			sales_order="SO-1", posting_date="2026-07-31", posting_time="10:00:00",
			historical_replay=True, allow_stock_supplement=True,
			items=[dict(crm_order_item_id="CRM-ROW-1", sales_order_item="SOI-1", item_code="ITEM-1",
				custom_version="V1", uom="Box", qty=3, source_unit_price=20, source_currency="USD")], **values)

	def order(self):
		return frappe._dict(name="SO-1", docstatus=1, company="Company", currency="USD", conversion_rate=7,
			custom_process_status="Deliverable", set_warehouse="HEADER-WH", items=[frappe._dict(
				name="SOI-1", item_code="ITEM-1", item_name="Item", description="Item", qty=10,
				delivered_qty=0, custom_crm_order_item_id="CRM-ROW-1", custom_version="V1", uom="Box",
				stock_uom="Nos", conversion_factor=2, rate=22, net_rate=20, base_net_rate=140,
				warehouse="FG-WH")])

	def plan(self, payload=None, sales_order=None, stock=4):
		payload = shipment.normalize_crm_shipment_payload(payload or self.source())
		order = sales_order or self.order()
		allocations = shipment.allocate_crm_shipment_items(order, payload["items"])
		with (
			patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup()),
			patch.object(frappe, "get_cached_value", return_value="CNY"),
			patch("erpnext.stock.utils.get_stock_balance", return_value=stock) as balance,
		):
			plan = shipment.build_crm_stock_plan(order, payload, allocations)
		return plan, balance

	def test_event_identity_is_organization_scoped_and_independent_of_human_batch(self):
		one = shipment.normalize_crm_shipment_payload(self.source())
		two_source = self.source()
		two_source["batch_no"] = "不同批号"
		two = shipment.normalize_crm_shipment_payload(two_source)
		three_source = self.source()
		three_source["source_organization"] = "OTHER"
		three = shipment.normalize_crm_shipment_payload(three_source)
		self.assertTrue(one[shipment.CRM_SHIPMENT_FIELD].startswith("crm-event-"))
		self.assertEqual(one[shipment.CRM_SHIPMENT_FIELD], two[shipment.CRM_SHIPMENT_FIELD])
		self.assertNotEqual(one[shipment.CRM_SHIPMENT_FIELD], three[shipment.CRM_SHIPMENT_FIELD])

	def test_historical_shipment_requires_true_posting_timestamp(self):
		payload = self.source()
		del payload["posting_time"]
		with self.assertRaisesRegex(frappe.ValidationError, "posting_date.*posting_time"):
			shipment.validate_crm_shipment_payload(shipment.normalize_crm_shipment_payload(payload))

	def test_exact_crm_row_identity_rejects_wrong_native_row(self):
		payload = self.source()
		payload["items"][0]["crm_order_item_id"] = "WRONG"
		with self.assertRaisesRegex(frappe.ValidationError, "crm_order_item_id"):
			shipment.allocate_crm_shipment_items(self.order(), payload["items"])

	def test_uom_must_match_native_order_row(self):
		payload = self.source()
		payload["items"][0]["uom"] = "Nos"
		with self.assertRaisesRegex(frappe.ValidationError, "uom"):
			shipment.allocate_crm_shipment_items(self.order(), payload["items"])

	def test_ambiguous_same_code_rows_require_exact_identity(self):
		order = self.order()
		other = frappe._dict(order["items"][0])
		other.name = "SOI-2"
		order["items"].append(other)
		with self.assertRaisesRegex(frappe.ValidationError, "sales_order_item|crm_order_item_id"):
			shipment.allocate_crm_shipment_items(order, [dict(item_code="ITEM-1", qty=1)], strict=True)

	def test_historical_native_balance_and_stock_uom_base_currency_shortfall(self):
		plan, balance = self.plan()
		row = plan["items"][0]
		self.assertEqual(row["warehouse"], "FG-WH")
		self.assertEqual(row["stock_qty"], 6)
		self.assertEqual(row["shortage_qty"], 2)
		self.assertEqual(row["temporary_valuation_rate"], 70)
		self.assertEqual(row["base_currency"], "CNY")
		balance.assert_called_once_with("ITEM-1", "FG-WH", "2026-07-31", "10:00:00")
		self.assertFalse(plan["blockers"])

	def test_duplicate_item_warehouse_rows_share_existing_stock_in_source_order(self):
		payload = self.source()
		payload["items"] = [dict(payload["items"][0], qty=2), dict(payload["items"][0], qty=2)]
		plan, balance = self.plan(payload, stock=5)
		self.assertEqual([row["shortage_qty"] for row in plan["items"]], [0, 3])
		self.assertEqual(balance.call_count, 1)

	def test_no_shortage_needs_no_supplement_price(self):
		payload = self.source()
		del payload["items"][0]["source_unit_price"]
		plan, _ = self.plan(payload, stock=10)
		self.assertFalse(plan["blockers"])
		self.assertEqual(plan["items"][0]["shortage_qty"], 0)

	def test_explicit_zero_is_preserved_and_missing_supplement_price_blocks(self):
		payload = self.source()
		payload["items"][0]["source_unit_price"] = 0
		order = self.order()
		order["items"][0].net_rate = 0
		order["items"][0].base_net_rate = 0
		plan, _ = self.plan(payload, order, stock=0)
		self.assertEqual(plan["items"][0]["temporary_valuation_rate"], 0)
		self.assertFalse(plan["blockers"])
		del payload["items"][0]["source_unit_price"]
		plan, _ = self.plan(payload, order, stock=0)
		self.assertIn("source_unit_price", " ".join(plan["blockers"]))

	def test_source_price_and_currency_must_agree_with_native_net_rate(self):
		payload = self.source()
		payload["items"][0]["source_unit_price"] = 22
		plan, _ = self.plan(payload, stock=0)
		self.assertIn("source_unit_price", " ".join(plan["blockers"]))
		payload["items"][0]["source_unit_price"] = 20
		payload["items"][0]["source_currency"] = "CNY"
		plan, _ = self.plan(payload, stock=0)
		self.assertIn("source_currency", " ".join(plan["blockers"]))

	def test_explicit_warehouse_conflict_blocks_instead_of_replacing_native_row(self):
		payload = self.source()
		payload["items"][0]["warehouse"] = "成品 - YC"
		plan, _ = self.plan(payload)
		self.assertIn("warehouse", " ".join(plan["blockers"]))
		self.assertEqual(plan["items"][0]["warehouse"], "FG-WH")

	def test_disabled_or_other_company_warehouse_blocks(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		order = self.order()
		allocations = shipment.allocate_crm_shipment_items(order, payload["items"])
		with patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup(company="OTHER", disabled=1)):
			with patch.object(frappe, "get_cached_value", return_value="CNY"):
				plan = shipment.build_crm_stock_plan(order, payload, allocations)
		self.assertTrue(plan["blockers"])

	def test_unreleased_order_is_blocked_without_status_mutation(self):
		order = self.order()
		order.custom_process_status = "Pending Final Payment"
		with patch.object(shipment, "set_process_status") as mutate:
			with self.assertRaisesRegex(frappe.ValidationError, "不允许|未放行"):
				shipment.release_sales_order_for_crm_shipment(order)
		mutate.assert_not_called()

	def test_historical_delivery_status_updates_do_not_enqueue_callbacks(self):
		doc = frappe._dict(name="DN-1", company="Company", docstatus=1, custom_crm_historical_replay=1)
		with (
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "get_linked_sales_orders", return_value=["SO-1"]),
			patch.object(frappe, "get_doc", return_value=self.order()),
			patch.object(shipment, "get_sales_order_delivery_process_status", return_value="Partially Delivered"),
			patch.object(shipment, "set_process_status"),
			patch.object(shipment, "enqueue_crm_delivery_note_shipment_event") as crm,
			patch.object(shipment, "enqueue_mes_delivery_note_status_callback") as mes,
		):
			shipment.update_sales_orders_delivery_process_status(doc)
		crm.assert_not_called()
		mes.assert_not_called()

	def test_existing_blank_crm_row_mapping_is_previewed_without_writes(self):
		order = self.order()
		order["items"][0].custom_crm_order_item_id = None
		allocations = shipment.allocate_crm_shipment_items(order, self.source()["items"])
		self.assertEqual(allocations[0].crm_order_item_id, "CRM-ROW-1")
		self.assertIsNone(order["items"][0].custom_crm_order_item_id)

	def test_preview_is_read_only_and_returns_source_native_fingerprints(self):
		plan, _ = self.plan()
		with (
			patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order()),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=None),
			patch.object(shipment, "build_crm_stock_plan", return_value=plan),
			patch.object(frappe, "get_doc") as write_doc,
			patch.object(frappe.db, "commit") as commit,
			patch.object(shipment, "create_crm_log") as log,
		):
			result = shipment.preview_delivery_note_from_crm(self.source())
		self.assertTrue(result["can_execute"])
		self.assertEqual(result["sales_order"], "SO-1")
		self.assertEqual(len(result["source_fingerprint"]), 64)
		self.assertEqual(len(result["preview_fingerprint"]), 64)
		write_doc.assert_not_called()
		commit.assert_not_called()
		log.assert_not_called()

	def test_preview_fingerprint_detects_stock_change_and_source_fingerprint_is_stable(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		plan, _ = self.plan(stock=4)
		changed, _ = self.plan(stock=1)
		first = shipment.get_crm_preview_response(self.order(), payload, plan)
		second = shipment.get_crm_preview_response(self.order(), payload, changed)
		self.assertEqual(first["source_fingerprint"], second["source_fingerprint"])
		self.assertNotEqual(first["preview_fingerprint"], second["preview_fingerprint"])
		payload["preview_fingerprint"] = first["preview_fingerprint"]
		with self.assertRaisesRegex(frappe.ValidationError, "预览.*变化|重新预览"):
			shipment.validate_crm_preview_fingerprints(payload, second)

	def test_native_supplement_only_receives_shortfall_before_shipment_timestamp(self):
		plan, _ = self.plan(stock=4)
		payload = shipment.normalize_crm_shipment_payload(self.source())
		entry = MagicMock()
		entry.name = "STE-1"
		with patch.object(frappe, "get_doc", return_value=entry) as native_doc, patch.object(frappe.db, "commit") as commit:
			result = shipment.make_crm_stock_supplement(self.order(), payload, plan)
		data = native_doc.call_args.args[0]
		self.assertEqual(data["stock_entry_type"], "CRM Outbound Temporary Receipt")
		self.assertEqual(data["purpose"], "Material Receipt")
		self.assertEqual(data["posting_date"], "2026-07-31")
		self.assertEqual(data["posting_time"], "09:59:59")
		self.assertEqual(data["items"][0]["qty"], 2)
		self.assertEqual(data["items"][0]["uom"], "Nos")
		self.assertEqual(data["items"][0]["basic_rate"], 70)
		self.assertEqual(data["items"][0]["set_basic_rate_manually"], 1)
		self.assertIn("临时", data["remarks"])
		entry.insert.assert_called_once_with(ignore_permissions=True)
		entry.submit.assert_called_once()
		commit.assert_not_called()
		self.assertIs(result, entry)

	def test_no_shortage_creates_no_stock_entry(self):
		plan, _ = self.plan(stock=10)
		with patch.object(frappe, "get_doc") as native_doc:
			self.assertIsNone(shipment.make_crm_stock_supplement(self.order(), self.source(), plan))
		native_doc.assert_not_called()

	def test_supplement_at_midnight_sorts_on_previous_day(self):
		payload = self.source()
		payload["posting_time"] = "00:00:00"
		plan, _ = self.plan(payload, stock=0)
		with patch.object(frappe, "get_doc", return_value=MagicMock()) as native_doc:
			shipment.make_crm_stock_supplement(self.order(), shipment.normalize_crm_shipment_payload(payload), plan)
		data = native_doc.call_args.args[0]
		self.assertEqual(data["posting_date"], "2026-07-30")
		self.assertEqual(data["posting_time"], "23:59:59")

	def test_supplement_explicit_zero_disables_valuation_fallback(self):
		plan, _ = self.plan(stock=0)
		plan["items"][0]["temporary_valuation_rate"] = 0
		with patch.object(frappe, "get_doc", return_value=MagicMock()) as native_doc:
			shipment.make_crm_stock_supplement(self.order(), shipment.normalize_crm_shipment_payload(self.source()), plan)
		self.assertEqual(native_doc.call_args.args[0]["items"][0]["allow_zero_valuation_rate"], 1)

	def test_submitted_replay_is_checked_before_stock_planning_and_supplement(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		doc = frappe._dict(name="DN-1", docstatus=1, custom_crm_source_fingerprint=shipment.get_crm_shipment_source_fingerprint(payload))
		with (
			patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order()),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "create_crm_log"), patch.object(shipment, "update_crm_log"),
			patch.object(shipment, "crm_shipment_lock", return_value=nullcontext()),
			patch.object(shipment, "lock_sales_order_for_crm_shipment"),
			patch.object(frappe, "get_doc", return_value=self.order()),
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=doc),
			patch.object(shipment, "validate_crm_delivery_note_identity"),
			patch.object(shipment, "build_crm_stock_plan") as plan,
			patch.object(shipment, "make_crm_stock_supplement") as supplement,
			patch.object(shipment, "get_crm_shipment_response", return_value={"idempotent_replay": True}),
		):
			result = shipment.create_and_submit_delivery_note_from_crm(payload)
		self.assertTrue(result["idempotent_replay"])
		plan.assert_not_called()
		supplement.assert_not_called()

	def test_exception_keeps_shipment_lock_until_database_rollback(self):
		after_commit, after_rollback = MagicMock(), MagicMock()
		with (
			patch.object(frappe.db, "db_type", "mariadb"),
			patch.object(frappe.db, "sql", return_value=[(1,)]) as sql,
			patch.object(frappe.db, "after_commit", after_commit),
			patch.object(frappe.db, "after_rollback", after_rollback),
		):
			with self.assertRaisesRegex(RuntimeError, "native submit failed"):
				with shipment.crm_shipment_lock("event"):
					raise RuntimeError("native submit failed")
			self.assertEqual(sql.call_count, 1)
			after_rollback.add.call_args.args[0]()
			self.assertEqual(sql.call_count, 2)

	def test_stock_pool_locks_are_sorted_and_held_to_transaction_end(self):
		rows = [dict(item_code="Z", warehouse="W"), dict(item_code="A", warehouse="W"), dict(item_code="A", warehouse="W")]
		with patch.object(shipment, "crm_shipment_lock", return_value=nullcontext()) as lock:
			with shipment.crm_stock_pool_locks(rows):
				pass
		self.assertEqual([call.args[0] for call in lock.call_args_list], ["stock:A:W", "stock:Z:W"])

	def test_native_submit_failure_rolls_back_supplement_mapping_and_delivery_savepoint(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		plan, _ = self.plan()
		preview = shipment.get_crm_preview_response(self.order(), payload, plan)
		payload.update(source_fingerprint=preview["source_fingerprint"], preview_fingerprint=preview["preview_fingerprint"])
		doc = MagicMock()
		doc.name = "DN-1"
		doc.docstatus = 0
		doc.submit.side_effect = RuntimeError("native accounting period closed")
		with (
			patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order()),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "create_crm_log"), patch.object(shipment, "update_crm_log"),
			patch.object(shipment, "crm_shipment_lock", return_value=nullcontext()),
			patch.object(shipment, "crm_stock_pool_locks", return_value=nullcontext()),
			patch.object(shipment, "lock_sales_order_for_crm_shipment"),
			patch.object(frappe, "get_doc", return_value=self.order()),
			patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup()),
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=None),
			patch.object(shipment, "build_crm_stock_plan", return_value=plan),
			patch.object(shipment, "persist_crm_order_item_mappings") as mapping,
			patch.object(shipment, "get_or_create_crm_delivery_note", return_value=(doc, False)),
			patch.object(shipment, "make_crm_stock_supplement", return_value=frappe._dict(name="STE-1")) as supplement,
			patch.object(shipment, "prepare_crm_delivery_note_for_submit"),
			patch.object(frappe.db, "savepoint") as savepoint,
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe.db, "commit") as commit,
		):
			with self.assertRaisesRegex(RuntimeError, "native accounting period closed"):
				shipment.create_and_submit_delivery_note_from_crm(payload)
		mapping.assert_called_once()
		supplement.assert_called_once()
		rollback.assert_called_once_with(save_point=savepoint.call_args.args[0])
		commit.assert_not_called()

	def test_response_reports_native_repost_pending_and_supplement_ids(self):
		doc = frappe._dict(name="DN-1", docstatus=1, custom_crm_stock_supplement="STE-1")
		with patch.object(shipment, "get_crm_repost_dependencies", return_value=[frappe._dict(name="RIV-1", status="Queued")]):
			result = shipment.get_crm_shipment_response(doc, "EVENT")
		self.assertEqual(result["supplement_ids"], ["STE-1"])
		self.assertTrue(result["stock_updated"])
		self.assertTrue(result["valuation_pending"])
		self.assertEqual(result["valuation_status"], "pending")

	def test_new_audit_schema_is_readonly_and_crm_row_mapping_allows_submitted_orders(self):
		from crm_integration.patches.add_crm_custom_fields import create_crm_custom_fields
		with patch("crm_integration.patches.add_crm_custom_fields.create_custom_fields") as create:
			create_crm_custom_fields()
		fields = create.call_args.args[0]
		so_fields = {field["fieldname"]: field for field in fields["Sales Order Item"]}
		self.assertEqual(so_fields["custom_crm_order_item_id"]["allow_on_submit"], 1)
		for doctype, names in {
			"Delivery Note": ["custom_crm_source_organization", "custom_crm_event_id", "custom_crm_batch_no", "custom_crm_historical_replay", "custom_crm_source_fingerprint", "custom_crm_stock_supplement"],
			"Stock Entry": ["custom_crm_shipment_no", "custom_crm_delivery_note", "custom_crm_valuation_policy"],
			"Delivery Note Item": ["custom_crm_order_item_id", "custom_crm_source_unit_price", "custom_crm_source_currency"],
		}.items():
			mapped = {field["fieldname"]: field for field in fields[doctype]}
			for name in names:
				self.assertEqual(mapped[name]["read_only"], 1)

	def test_execute_reads_native_historical_balance_with_current_row_lock(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		order = self.order()
		allocations = shipment.allocate_crm_shipment_items(order, payload["items"])
		with (
			patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup()),
			patch.object(frappe, "get_cached_value", return_value="CNY"),
			patch("erpnext.stock.stock_ledger.get_previous_sle", return_value=frappe._dict(qty_after_transaction=5)) as current,
			patch("erpnext.stock.utils.get_stock_balance") as stale_snapshot,
		):
			plan = shipment.build_crm_stock_plan(order, payload, allocations, for_update=True)
		self.assertEqual(plan["items"][0]["shortage_qty"], 1)
		self.assertTrue(current.call_args.kwargs["for_update"])
		self.assertEqual(current.call_args.args[0]["posting_date"], "2026-07-31")
		stale_snapshot.assert_not_called()

	def test_invalid_text_price_is_not_accepted_as_explicit_zero(self):
		payload = self.source()
		payload["items"][0]["source_unit_price"] = "invalid"
		order = self.order()
		order["items"][0].net_rate = 0
		order["items"][0].base_net_rate = 0
		plan, _ = self.plan(payload, order, stock=0)
		self.assertIn("source_unit_price", " ".join(plan["blockers"]))

	def test_legacy_release_policy_remains_scoped_to_legacy_callers(self):
		order = self.order()
		order.custom_process_status = "Pending Production"
		with patch.object(shipment, "set_process_status") as release:
			shipment.release_sales_order_for_crm_shipment(order, legacy_release=True)
		release.assert_called_once_with(order, "Deliverable")

	def test_preview_rejects_two_crm_rows_binding_one_blank_native_row(self):
		order = self.order()
		order["items"][0].custom_crm_order_item_id = None
		items = [dict(self.source()["items"][0], qty=1), dict(self.source()["items"][0], qty=1, crm_order_item_id="CRM-ROW-2")]
		with self.assertRaisesRegex(frappe.ValidationError, "crm_order_item_id"):
			shipment.allocate_crm_shipment_items(order, items)

	def test_missing_native_uom_conversion_blocks_supplement(self):
		order = self.order()
		order["items"][0].conversion_factor = 0
		plan, _ = self.plan(sales_order=order, stock=0)
		self.assertTrue(plan["blockers"])

	def test_sales_order_identity_conflict_is_not_reported_as_missing_order(self):
		with patch.object(shipment, "get_crm_shipment_sales_order", side_effect=shipment.CRMShipmentIdentityConflict("wrong company/order identity")):
			preview = shipment.preview_delivery_note_from_crm(self.source())
		self.assertEqual(preview["blocker_code"], "ERP_CRM_SHIPMENT_IDENTITY_CONFLICT")

	def test_empty_native_ledger_has_identical_preview_and_locked_execution_fingerprint(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		order = self.order()
		with (
			patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup()),
			patch.object(frappe, "get_cached_value", return_value="CNY"),
			patch("erpnext.stock.stock_ledger.get_previous_sle", return_value=None),
			patch("erpnext.stock.utils.get_stock_balance", return_value=0.0),
		):
			preview = shipment.build_crm_stock_plan(order, payload, shipment.allocate_crm_shipment_items(order,payload["items"]))
			locked = shipment.build_crm_stock_plan(order, payload, shipment.allocate_crm_shipment_items(order,payload["items"]),for_update=True)
		self.assertEqual(shipment.get_crm_preview_response(order,payload,preview)["preview_fingerprint"],
			shipment.get_crm_preview_response(order,payload,locked)["preview_fingerprint"])

	def test_false_new_contract_flags_still_use_strict_release_policy(self):
		self.assertTrue(shipment.is_strict_crm_shipment(dict(shipment_no="OLD", historical_replay=False)))
		self.assertTrue(shipment.is_strict_crm_shipment(dict(shipment_no="OLD", allow_stock_supplement=False)))

	def test_strict_first_execute_requires_full_source_identity_and_both_preview_tokens(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		with self.assertRaisesRegex(frappe.ValidationError, "fingerprint"):
			shipment.validate_crm_strict_execution_payload(payload)
		payload.update(source_fingerprint="source", preview_fingerprint="preview")
		for field in ("crm_order_item_id", "item_code", "custom_version", "uom"):
			missing = dict(payload, items=[dict(payload["items"][0])])
			del missing["items"][0][field]
			with self.assertRaisesRegex(frappe.ValidationError, field):
				shipment.validate_crm_strict_execution_payload(missing)

	def test_custom_temporary_receipt_type_is_created_idempotently_and_never_repurposed(self):
		from crm_integration.patches import add_crm_custom_fields as schema
		with patch.object(frappe.db, "exists", return_value=False), patch.object(frappe, "get_doc") as create:
			schema.ensure_crm_temporary_receipt_type()
		self.assertEqual(create.call_args.args[0]["name"], "CRM Outbound Temporary Receipt")
		self.assertEqual(create.call_args.args[0]["purpose"], "Material Receipt")
		with patch.object(frappe.db, "exists", return_value=True), patch.object(frappe.db, "get_value", return_value="Material Issue"), patch.object(frappe, "get_doc") as create:
			with self.assertRaisesRegex(frappe.ValidationError, "purpose"):
				schema.ensure_crm_temporary_receipt_type()
		create.assert_not_called()

	def test_temporary_receipt_does_not_trigger_mes_receipt_callback_in_token_context(self):
		try:
			from mes_integration.mes_integration import stock_entry as mes_stock
		except ModuleNotFoundError:
			self.skipTest("Optional MES integration app is not installed")
		doc = frappe._dict(stock_entry_type="CRM Outbound Temporary Receipt", purpose="Material Receipt", company="Company", docstatus=1, flags=frappe._dict())
		with (patch.object(mes_stock, "is_mes_integration_enabled", return_value=True),
			patch.object(frappe.local, "request", object(), create=True),
			patch.object(frappe, "get_request_header", return_value="token QA-NOT-A-SECRET"),
			patch.object(frappe.db, "after_commit") as callbacks):
			self.assertFalse(mes_stock.is_mes_receipt_stock_entry(doc))
			mes_stock.notify_mes_stock_entry_status(doc)
		callbacks.add.assert_not_called()

	def test_locked_order_header_clears_stale_tax_template_and_tax_rows(self):
		order = frappe.get_doc(dict(doctype="Sales Order", docstatus=1, company="Company", currency="MXN", conversion_rate=7,
			taxes_and_charges=None, taxes=[], sales_team=[]))
		delivery = frappe.get_doc(dict(doctype="Delivery Note", company="OLD", currency="USD", conversion_rate=1,
			taxes_and_charges="STALE", taxes=[dict(charge_type="Actual", tax_amount=5)], sales_team=[]))
		shipment.apply_locked_sales_order_header(delivery, order)
		self.assertEqual(delivery.company, "Company")
		self.assertEqual(delivery.currency, "MXN")
		self.assertEqual(delivery.conversion_rate, 7)
		self.assertIsNone(delivery.taxes_and_charges)
		self.assertEqual(delivery.taxes, [])

	def test_incomplete_strict_preview_returns_native_mapping_and_contract_blockers(self):
		payload = self.source()
		del payload["items"][0]["uom"]
		plan, _ = self.plan(payload)
		with (patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order()),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=None),
			patch.object(shipment, "build_crm_stock_plan", return_value=plan)):
			preview = shipment.preview_delivery_note_from_crm(payload)
		self.assertFalse(preview["can_execute"])
		self.assertIn("uom", " ".join(preview["blockers"]))
		self.assertEqual(preview["sales_order_items"][0]["uom"], "Box")

	def test_strict_event_cannot_execute_using_only_a_human_shipment_number(self):
		payload = self.source()
		del payload["source_organization"]
		del payload["event_id"]
		payload.update(custom_crm_shipment_no="HUMAN-BATCH", source_fingerprint="source", preview_fingerprint="preview")
		with self.assertRaisesRegex(frappe.ValidationError, "source_organization|event_id"):
			shipment.validate_crm_strict_execution_payload(payload)

	def test_direct_mes_enqueue_wrapper_checks_persisted_historical_marker(self):
		try:
			from mes_integration.mes_integration import delivery_note as mes_delivery
		except ModuleNotFoundError:
			self.skipTest("Optional MES integration app is not installed")
		with (patch.object(frappe.db, "get_value", return_value=1),
			patch.object(mes_delivery, "enqueue_delivery_note_status_callback") as enqueue):
			shipment.enqueue_mes_delivery_note_status_callback("HISTORICAL-DN")
		enqueue.assert_not_called()

	def test_strict_shipment_never_fills_blank_native_warehouse_from_request(self):
		order = self.order()
		order["items"][0].warehouse = None
		order.set_warehouse = None
		payload = self.source()
		payload["items"][0]["warehouse"] = "EXPLICIT-REQUEST-WH"
		plan, balance = self.plan(payload, order)
		self.assertTrue(plan["blockers"])
		self.assertIsNone(plan["items"][0]["warehouse"])
		balance.assert_not_called()

	def test_legacy_stock_shortage_is_left_to_native_negative_stock_validation(self):
		payload = dict(custom_crm_shipment_no="LEGACY", sales_order="SO-1", items=[dict(sales_order_item="SOI-1", item_code="ITEM-1", qty=3)])
		plan, _ = self.plan(payload, stock=-2)
		self.assertFalse(plan["blockers"])
		self.assertEqual(plan["items"][0]["shortage_qty"], 6)

	def test_legacy_explicit_warehouse_retains_existing_native_request_priority(self):
		payload = dict(custom_crm_shipment_no="LEGACY", sales_order="SO-1", items=[dict(sales_order_item="SOI-1", item_code="ITEM-1", qty=1, warehouse="REQUEST-WH")])
		plan, _ = self.plan(payload, stock=10)
		self.assertFalse(plan["blockers"])
		self.assertEqual(plan["items"][0]["warehouse"], "REQUEST-WH")

	def test_legacy_same_code_version_quantity_can_split_across_existing_order_rows(self):
		order = self.order()
		second = frappe._dict(order["items"][0])
		second.name = "SOI-2"
		second.custom_crm_order_item_id = "CRM-ROW-2"
		order["items"].append(second)
		allocations = shipment.allocate_crm_shipment_items(order, [dict(item_code="ITEM-1", qty=12, uom="LEGACY-IGNORED")])
		self.assertEqual([row.qty for row in allocations], [10, 2])

	def test_native_base_rate_rounding_respects_currency_field_precision(self):
		order = self.order()
		order.conversion_rate = 17.123456
		order["items"][0].net_rate = 18.75
		order["items"][0].base_net_rate = 321.06
		payload = self.source()
		payload["items"][0]["source_unit_price"] = 18.75
		plan, _ = self.plan(payload, order, stock=0)
		self.assertFalse(plan["blockers"])
		self.assertEqual(plan["items"][0]["temporary_valuation_rate"], 160.53)

	def test_preview_fingerprint_includes_commercial_header_and_tax_changes(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		plan, _ = self.plan()
		order = self.order()
		order.taxes = [dict(charge_type="On Net Total", rate=16)]
		order.discount_amount = 0
		first = shipment.get_crm_preview_response(order,payload,plan)
		order.taxes[0]["rate"] = 8
		order.discount_amount = 5
		second = shipment.get_crm_preview_response(order,payload,plan)
		self.assertNotEqual(first["preview_fingerprint"], second["preview_fingerprint"])

	def test_receipt_reports_item_based_repost_without_any_voucher_reference(self):
		doc = frappe._dict(name="DN-1", company="Company", docstatus=1, items=[frappe._dict(item_code="ITEM-1", warehouse="FG-WH")])
		queued = frappe._dict(name="RPV-ITEM-1", status="Queued", based_on="Item and Warehouse", item_code="ITEM-1", warehouse="FG-WH", voucher_type=None, voucher_no=None)
		with patch.object(frappe.db, "sql", return_value=[queued]) as native_query, patch.object(frappe, "get_all", return_value=[]):
			result = shipment.get_crm_shipment_response(doc, "EVENT")
		self.assertTrue(result["valuation_pending"])
		self.assertEqual(result["repost_status"][0]["name"], "RPV-ITEM-1")
		query, values = native_query.call_args.args
		self.assertIn("item_code", query)
		self.assertIn("warehouse", query)
		self.assertIn("ITEM-1", values)
		self.assertIn("FG-WH", values)

	def test_actual_created_repost_ids_are_persisted_for_later_replay_status(self):
		doc = MagicMock()
		doc.name = "DN-1"
		with patch.object(shipment, "get_crm_repost_dependencies", return_value=[frappe._dict(name="RPV-1", status="Queued")]) as dependencies:
			shipment.record_crm_repost_dependencies(doc, "2026-10-15 12:00:00")
		self.assertTrue(dependencies.call_args.kwargs["for_update"])
		self.assertEqual(dependencies.call_args.kwargs["created_since"], "2026-10-15 12:00:00")
		doc.db_set.assert_called_once_with("custom_crm_repost_ids", '["RPV-1"]', update_modified=False)

	def test_commercial_order_version_ignores_delivery_progress_but_catches_tax_changes(self):
		order = frappe._dict(name="SO-VERSION", company="QA", currency="MXN", conversion_rate=1,
			taxes=[dict(rate=16)], items=[dict(name="ROW", item_code="SKU", qty=10, rate=18, delivered_qty=0)])
		first = shipment.get_crm_commercial_order_fingerprint(order)
		order.status = "Completed"
		order.modified = "2026-10-15"
		order.custom_process_status = "Delivered"
		order["items"][0]["delivered_qty"] = 2
		self.assertEqual(first, shipment.get_crm_commercial_order_fingerprint(order))
		order.taxes[0]["rate"] = 8
		self.assertNotEqual(first, shipment.get_crm_commercial_order_fingerprint(order))

	def test_commercial_order_version_accepts_native_tax_child_documents(self):
		order = frappe.new_doc("Sales Order")
		order.company = "QA"
		order.append("taxes", dict(charge_type="On Net Total", account_head="QA Tax", rate=16))
		first = shipment.get_crm_commercial_order_fingerprint(order)
		order.taxes[0].rate = 8
		self.assertNotEqual(first, shipment.get_crm_commercial_order_fingerprint(order))

	def test_frozen_committed_receipt_is_read_before_mutable_sales_order(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		payload["source_fingerprint"] = shipment.get_crm_shipment_source_fingerprint(payload)
		delivery = frappe._dict(name="DN-COMMITTED", docstatus=1, company="Company", currency="USD",
			custom_crm_source_fingerprint=payload["source_fingerprint"], items=[])
		with (
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=delivery),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "get_crm_shipment_response", return_value=dict(name="DN-COMMITTED", stock_updated=True, idempotent_replay=True)),
			patch.object(shipment, "get_crm_shipment_sales_order", side_effect=frappe.ValidationError("order closed")) as current,
		):
			result = shipment.preview_delivery_note_from_crm(payload)
			self.assertEqual(result.get("name"), "DN-COMMITTED")
			self.assertTrue(result.get("stock_updated"))
			current.assert_not_called()

	def test_finished_goods_require_enabled_inventory_sales_item_even_with_stock(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		order = self.order()
		for conflict in (dict(is_stock_item=0), dict(disabled=1), dict(is_sales_item=0), dict(has_variants=1)):
			with (
				self.subTest(conflict=conflict),
				patch.object(frappe.db, "get_value", side_effect=self.warehouse_lookup(item_values=conflict)),
				patch.object(frappe, "get_cached_value", return_value="CNY"),
				patch("erpnext.stock.utils.get_stock_balance", return_value=100),
			):
				plan = shipment.build_crm_stock_plan(order, payload, shipment.allocate_crm_shipment_items(order,payload["items"]))
				self.assertTrue(any("库存销售物料" in reason for reason in plan["blockers"]))

	def test_historical_parent_preview_names_notification_block_without_sending(self):
		with (
			patch.object(frappe.db,"exists",return_value=True),
			patch.object(frappe,"get_all",return_value=[frappe._dict(name="SO通知",company=None)]),
		):
			blockers = shipment.get_crm_historical_parent_creation_blockers("Company")
		self.assertTrue(any("通知" in reason for reason in blockers))

	def test_mapping_preview_returns_read_only_full_manifest_completion_proof(self):
		payload = self.source()
		payload["_crm_order_items"] = [dict(id="CRM-ROW-1", itemCode="SKU", quantity=10, unit="件", internalEvidence="retained")]
		proof = dict(can_complete=True, completion_fingerprint="proof", added_items=[dict(item_code="SKU")], blockers=[])
		with (
			patch.object(shipment, "get_crm_shipment_sales_order", return_value=self.order()),
			patch.object(shipment, "is_crm_integration_enabled", return_value=True),
			patch.object(shipment, "get_delivery_note_by_crm_shipment_no", return_value=None),
			patch.object(shipment, "allocate_crm_shipment_items", side_effect=frappe.ValidationError("missing row")),
			patch("crm_integration.crm_integration.api.plan_missing_crm_sales_order_items", return_value=proof) as planner,
			patch.object(frappe.db, "commit") as commit,
			patch.object(shipment, "create_crm_log") as log,
		):
			view = shipment.preview_delivery_note_from_crm(payload)
		self.assertEqual(view["order_completion"], proof)
		self.assertNotIn("internalEvidence", planner.call_args.args[1][0])
		commit.assert_not_called()
		log.assert_not_called()

	def test_commercial_order_version_includes_settlement_decisions(self):
		order = self.order()
		first = shipment.get_crm_commercial_order_fingerprint(order)
		order.custom_china_settlement_mode = "direct_receivable"
		self.assertNotEqual(first, shipment.get_crm_commercial_order_fingerprint(order))
		first = shipment.get_crm_commercial_order_fingerprint(order)
		order.custom_china_settlement_confirmation_method = "external_customer_confirmation"
		self.assertNotEqual(first, shipment.get_crm_commercial_order_fingerprint(order))

	def test_optional_parent_company_does_not_change_preexisting_source_hash(self):
		payload = shipment.normalize_crm_shipment_payload(self.source())
		legacy = {key: payload.get(key) for key in (shipment.CRM_SHIPMENT_FIELD, "source_organization",
			"event_id", "batch_no", "sales_order", "custom_crm_order_no", "posting_date", "posting_time",
			"historical_replay", "allow_stock_supplement", "items")}
		self.assertEqual(shipment.get_crm_shipment_source_fingerprint(payload),shipment.crm_shipment_fingerprint(legacy))
		payload["parent_company"] = "Company"
		self.assertNotEqual(shipment.get_crm_shipment_source_fingerprint(payload),shipment.crm_shipment_fingerprint(legacy))
