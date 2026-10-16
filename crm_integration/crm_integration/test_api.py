from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from crm_integration.crm_integration import api
from crm_integration.crm_integration.api import (
	create_and_submit_sales_order,
	get_existing_sales_order_response,
	update_sales_order_by_crm_order_no,
	update_sales_order_items,
	validate_sales_order_payload,
	validate_sales_order_update_payload,
)


class TestCRMAPI(UnitTestCase):
	def test_preview_shipment_uses_same_authenticated_request_parser(self):
		payload = {"shipment_no": "SHIP-1"}
		with (patch.object(api, "validate_crm_api_user") as auth,
			patch.object(api, "get_shipment_request_payload", return_value=payload),
			patch.object(api, "preview_delivery_note_from_crm", return_value={"can_execute": False}) as preview):
			result = api.preview_delivery_note("wrapped")
		auth.assert_called_once()
		preview.assert_called_once_with(payload)
		self.assertFalse(result["can_execute"])

	def test_sales_order_create_requires_crm_order_no(self):
		with self.assertRaises(frappe.ValidationError):
			validate_sales_order_payload(
				{
					"customer": "CUSTOMER-001",
					"items": [{"item_code": "ITEM-001", "qty": 1}],
				}
			)

	def test_sales_order_create_returns_existing_submitted_order(self):
		doc = MagicMock(name="existing_sales_order")
		doc.name = "SO-001"
		doc.docstatus = 1
		doc.get.side_effect = lambda field: {
			"company": "Test Company",
			"custom_process_status": "Pending Deposit Confirmation",
		}.get(field)

		with (
			patch(
				"crm_integration.crm_integration.api.get_sales_order_names_by_crm_order_no",
				return_value=["SO-001"],
			),
			patch.object(frappe, "get_doc", return_value=doc),
		):
			result = get_existing_sales_order_response(
				{"custom_crm_order_no": "CRM-001", "company": "Test Company"}
			)

		self.assertTrue(result["idempotent_replay"])
		self.assertEqual(result["name"], "SO-001")

	def test_idempotent_replay_does_not_create_another_sales_order(self):
		payload = {
			"doctype": "Sales Order",
			"company": "Test Company",
			"customer": "CUSTOMER-001",
			"custom_crm_order_no": "CRM-001",
			"items": [{"item_code": "ITEM-001", "qty": 1}],
		}
		existing_response = {
			"status": "success",
			"name": "SO-001",
			"docstatus": 1,
			"idempotent_replay": True,
		}

		with (
			patch("crm_integration.crm_integration.api.validate_crm_api_user"),
			patch(
				"crm_integration.crm_integration.api.is_crm_integration_enabled",
				return_value=True,
			),
			patch(
				"crm_integration.crm_integration.api.crm_sales_order_creation_lock",
				return_value=nullcontext(),
			),
			patch(
				"crm_integration.crm_integration.api.get_existing_sales_order_response",
				return_value=existing_response,
			),
			patch("crm_integration.crm_integration.api.log_existing_sales_order_request"),
			patch("crm_integration.crm_integration.api.create_sales_order") as create_sales_order,
		):
			result = create_and_submit_sales_order(payload)

		self.assertEqual(result, existing_response)
		create_sales_order.assert_not_called()

	def test_sales_order_update_rejects_unsupported_header_fields(self):
		with self.assertRaises(frappe.ValidationError):
			validate_sales_order_update_payload(
				{
					"custom_crm_order_no": "CRM-001",
					"custom_process_status": "Completed",
				}
			)

	def test_sales_order_update_accepts_explicitly_allowed_fields(self):
		validate_sales_order_update_payload(
			{
				"custom_crm_order_no": "CRM-001",
				"custom_odt": "ODT-001",
				"custom_remark": "updated",
				"items": [{"item_code": "ITEM-001", "qty": 2}],
			}
		)

	def test_sales_order_update_requires_enabled_company(self):
		doc = MagicMock()
		doc.get.return_value = "Disabled Company"

		with (
			patch("crm_integration.crm_integration.api.validate_crm_api_user"),
			patch(
				"crm_integration.crm_integration.api.get_request_payload",
				return_value={"custom_crm_order_no": "CRM-001"},
			),
			patch.object(frappe, "get_all", side_effect=lambda doctype, **kwargs: ["SO-001"] if doctype == "Sales Order" else []),
			patch.object(frappe, "get_doc", return_value=doc),
			patch(
				"crm_integration.crm_integration.api.is_crm_integration_enabled",
				return_value=False,
			),
			self.assertRaises(frappe.ValidationError),
		):
			update_sales_order_by_crm_order_no()

		doc.save.assert_not_called()

	def test_sales_order_update_selects_effective_order_after_resubmission(self):
		doc = MagicMock()
		doc.name, doc.docstatus, doc.custom_crm_order_no = "SO-NEW", 1, "CRM-001"
		doc.flags = frappe._dict()
		doc.get.side_effect = lambda field: {"company": "Test Company"}.get(field)
		payload = {"custom_crm_order_no": "CRM-001", "custom_remark": "corrected"}
		with (
			patch.object(api, "validate_crm_api_user"),
			patch.object(api, "get_sales_order_names_by_crm_order_no", return_value=["SO-NEW"]) as names,
			patch.object(frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(api, "is_crm_integration_enabled", return_value=True),
			patch.object(api, "validate_sales_order_update_status"),
			patch("crm_integration.crm_integration.order_revision.linked_facts", return_value={}),
			patch("crm_integration.crm_integration.order_revision.eligibility", return_value=(None, None)),
		):
			result = update_sales_order_by_crm_order_no(payload)
		names.assert_called_once_with("CRM-001", docstatus=1)
		get_doc.assert_called_once_with("Sales Order", "SO-NEW")
		self.assertEqual(result["name"], "SO-NEW")
		doc.save.assert_called_once()

	def test_effective_order_lookup_filters_cancelled_history(self):
		with patch.object(frappe, "get_all", return_value=["SO-NEW"]) as get_all:
			self.assertEqual(api.get_sales_order_names_by_crm_order_no("CRM-001", docstatus=1), ["SO-NEW"])
		get_all.assert_called_once_with("Sales Order", filters={
			"custom_crm_order_no": "CRM-001", "docstatus": 1,
		}, pluck="name", limit_page_length=2)

	def test_sales_order_update_rejects_duplicate_effective_orders(self):
		with (
			patch.object(api, "validate_crm_api_user"),
			patch.object(api, "get_sales_order_names_by_crm_order_no", return_value=["SO-A", "SO-B"]) as names,
			patch.object(frappe, "get_doc") as get_doc,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "多笔有效销售订单"):
				update_sales_order_by_crm_order_no({"custom_crm_order_no": "CRM-001"})
		names.assert_called_once_with("CRM-001", docstatus=1)
		get_doc.assert_not_called()

	def test_sales_order_item_correction_requires_row_name(self):
		row = frappe._dict(name="SOI-001", item_code="ITEM-OLD")
		doc = MagicMock()
		doc.name = "SO-001"
		doc.items = [row]
		doc.get.side_effect = lambda fieldname: {
			"custom_process_status": "Pending Deposit Confirmation"
		}.get(fieldname)

		with self.assertRaisesRegex(frappe.ValidationError, "必须传明细 name"):
			update_sales_order_items(
				doc,
				[{"item_code": "ITEM-OLD", "new_item_code": "ITEM-NEW"}],
			)

	def test_sales_order_item_correction_is_rejected_after_mes_push(self):
		row = frappe._dict(name="SOI-001", item_code="ITEM-OLD")
		doc = MagicMock()
		doc.name = "SO-001"
		doc.items = [row]
		doc.get.side_effect = lambda fieldname: {
			"custom_process_status": "Pending Production"
		}.get(fieldname)

		with self.assertRaisesRegex(frappe.ValidationError, "只能在推送 MES 前更正"):
			update_sales_order_items(
				doc,
				[{"name": row.name, "new_item_code": "ITEM-NEW"}],
			)

	def test_sales_order_item_correction_is_rejected_with_downstream_qty(self):
		row = frappe._dict(name="SOI-001", item_code="ITEM-OLD", delivered_qty=1)
		doc = MagicMock()
		doc.name = "SO-001"
		doc.items = [row]
		doc.get.side_effect = lambda fieldname: {
			"custom_process_status": "Pending Deposit Confirmation"
		}.get(fieldname)

		with self.assertRaisesRegex(frappe.ValidationError, "已产生下游数量"):
			update_sales_order_items(
				doc,
				[{"name": row.name, "new_item_code": "ITEM-NEW"}],
			)

	@patch("crm_integration.crm_integration.api.replace_sales_order_item")
	def test_sales_order_item_correction_replaces_item_and_version(self, replace_item):
		row = frappe._dict(name="SOI-001", item_code="ITEM-OLD", custom_version="OLD")
		row.set = lambda fieldname, value: row.__setitem__(fieldname, value)
		doc = MagicMock()
		doc.name = "SO-001"
		doc.items = [row]
		doc.get.side_effect = lambda fieldname: {
			"custom_process_status": "Pending Deposit Confirmation"
		}.get(fieldname)

		update_sales_order_items(
			doc,
			[
				{
					"name": row.name,
					"item_code": "ITEM-OLD",
					"new_item_code": "ITEM-NEW",
					"custom_version": "PC_PRINT",
					"qty": 8,
					"rate": 0,
				},
			],
		)

		replace_item.assert_called_once_with(doc, row, "ITEM-NEW")
		self.assertEqual(row.custom_version, "PC_PRINT")
		self.assertEqual(row.qty, 8)
		self.assertEqual(row.rate, 0)
		self.assertEqual(row.price_list_rate, 0)

	def test_sales_order_item_name_and_item_code_must_match(self):
		row = frappe._dict(name="SOI-001", item_code="ITEM-OLD")
		doc = MagicMock()
		doc.name = "SO-001"
		doc.items = [row]
		doc.get.side_effect = lambda fieldname: {
			"custom_process_status": "Pending Deposit Confirmation"
		}.get(fieldname)

		with self.assertRaisesRegex(frappe.ValidationError, "与请求中的 item_code"):
			update_sales_order_items(
				doc,
				[{"name": row.name, "item_code": "ITEM-WRONG", "qty": 2}],
			)


class TestMissingCRMOrderItems(UnitTestCase):
	def setUp(self):
		self.doc = frappe._dict(name="SO-001", doctype="Sales Order", docstatus=1,
			company="Test Company", custom_crm_order_no="CRM-001", custom_process_status="Pending Production",
			status="To Deliver and Bill", delivery_date="2026-10-15", set_warehouse="Stores - TC",
			items=[frappe._dict(name="SOI-001", item_code="ITEM-001", custom_version="V1", uom="Nos",
				stock_uom="Nos", conversion_factor=1, qty=2, rate=10, net_rate=10,
				delivery_date="2026-10-15", description="Human description", warehouse="Stores - TC",
				custom_product="Human product", custom_remark="Keep me", delivered_qty=1)], taxes=[])
		self.source = [self.source_row("CRM-ROW-1", "ITEM-001", 2, 10),
			self.source_row("CRM-ROW-2", "ITEM-002", 3, 0)]
		self.item = frappe._dict(name="ITEM-002", item_code="ITEM-002", disabled=0, is_stock_item=1,
			is_sales_item=1, stock_uom="Nos", description="Native item", uoms=[], taxes=[],
			item_defaults=[frappe._dict(company="Test Company", default_warehouse="Stores - TC")])

	@staticmethod
	def source_row(identity, code, qty, price):
		return dict(id=identity, itemCode=code, version="V1", unit="件", quantity=qty,
			unitPrice=price, supplyPrice=0, vatAmount=0, lineNo=1)

	def plan(self, doc=None, source=None):
		planner = getattr(api, "plan_missing_crm_sales_order_items", None)
		self.assertTrue(callable(planner), "read-only missing-row planner must be exposed")
		with (patch.object(frappe, "get_cached_doc", return_value=self.item),
			patch.object(frappe.db, "get_value", return_value=frappe._dict(company="Test Company", disabled=0, is_group=0)),
			patch.object(api, "get_item_warehouse_", return_value="Stores - TC", create=True)):
			return planner(doc or self.doc, self.source if source is None else source)

	def test_true_missing_row_preserves_existing_manual_values(self):
		before = deepcopy(self.doc)
		plan = self.plan()
		self.assertTrue(plan["can_complete"], plan["blockers"])
		self.assertEqual(len(plan["added_items"]), 1)
		self.assertEqual(plan["added_items"][0]["item_code"], "ITEM-002")
		self.assertEqual(plan["added_items"][0]["rate"], 0)
		self.assertEqual(plan["added_items"][0]["uom"], "Nos")
		self.assertEqual(self.doc, before)

	def test_unmatched_old_item_blocks_replacement_append(self):
		self.doc["items"][0].item_code = "OLD-CODE"
		plan = self.plan()
		self.assertFalse(plan["can_complete"])
		self.assertTrue(any("SOI-001" in reason for reason in plan["blockers"]))

	def test_bound_row_still_requires_unchanged_source_commercial_values(self):
		for field, value in (("itemCode", "NEW-CODE"), ("version", "V2"), ("quantity", 20),
			("unitPrice", 11), ("unit", "Box")):
			with self.subTest(field=field):
				source = deepcopy(self.source)
				source[0][field] = value
				self.doc["items"][0].custom_crm_order_item_id = "CRM-ROW-1"
				plan = self.plan(source=source)
				self.assertFalse(plan["can_complete"])
				self.assertTrue(any("SOI-001" in reason for reason in plan["blockers"]))

	def test_fully_mapped_manifest_does_not_offer_empty_completion(self):
		self.doc.custom_process_status = "Released"
		plan = self.plan(source=self.source[:1])
		self.assertFalse(plan["can_complete"])
		self.assertEqual(plan["blockers"], [])

	def test_missing_version_is_not_an_explicit_blank_version(self):
		del self.source[1]["version"]
		self.assertFalse(self.plan()["can_complete"])

	def test_historical_completion_blocks_value_change_notifications(self):
		with (patch("crm_integration.crm_integration.delivery_note.get_crm_historical_order_completion_blockers",
			return_value=["历史补行通知必须先核对"]), patch.object(frappe, "get_cached_doc", return_value=self.item)):
			plan = api.plan_missing_crm_sales_order_items(self.doc, self.source, historical_replay=True)
		self.assertFalse(plan["can_complete"])
		self.assertIn("历史补行通知必须先核对", plan["blockers"])

	def test_missing_price_is_blocked_while_explicit_zero_is_supported(self):
		del self.source[1]["unitPrice"]
		plan = self.plan()
		self.assertFalse(plan["can_complete"])
		self.assertTrue(any("price" in reason.lower() for reason in plan["blockers"]))

	def test_unknown_uom_cannot_be_coerced_to_nos(self):
		self.source[1]["unit"] = "台"
		plan = self.plan()
		self.assertFalse(plan["can_complete"])
		self.assertTrue(any("UOM" in reason for reason in plan["blockers"]))

	def test_explicit_item_uom_factor_allows_unknown_label_without_coercion(self):
		self.source[1]["unit"] = "台"
		self.item.uoms = [frappe._dict(uom="台", conversion_factor=2)]
		plan = self.plan()
		self.assertTrue(plan["can_complete"], plan["blockers"])
		self.assertEqual(plan["added_items"][0]["uom"], "台")
		self.assertEqual(plan["added_items"][0]["conversion_factor"], 2)

	def test_company_so_header_warehouse_keeps_native_precedence(self):
		self.item.item_defaults[0].default_warehouse = "Other Stores - TC"
		plan = self.plan()
		self.assertTrue(plan["can_complete"], plan["blockers"])
		self.assertEqual(plan["added_items"][0]["warehouse"], "Stores - TC")

	def test_changed_native_item_uom_proof_invalidates_completion_preview(self):
		self.source[1]["unit"] = "台"
		self.item.uoms = [frappe._dict(uom="台", conversion_factor=2)]
		first = self.plan()["completion_fingerprint"]
		self.item.uoms[0].conversion_factor = 3
		self.assertNotEqual(first, self.plan()["completion_fingerprint"])

	def test_piece_alias_cannot_override_conflicting_configured_item_factor(self):
		self.source[0]["unit"] = "PCS"
		self.item.uoms = [frappe._dict(uom="PCS", conversion_factor=10)]
		self.assertFalse(self.plan()["can_complete"])

	def test_full_manifest_and_existing_commercial_values_version_completion(self):
		first = self.plan()["completion_fingerprint"]
		self.source[0]["lineNo"] = 99
		second = self.plan()["completion_fingerprint"]
		self.assertNotEqual(first, second)
		self.doc["items"][0].rate = 11
		self.assertNotEqual(second, self.plan()["completion_fingerprint"])

	def test_ambiguous_unbound_source_rows_block_completion(self):
		self.source.insert(1, dict(self.source[0], id="DUPLICATE"))
		self.assertFalse(self.plan()["can_complete"])

	def test_duplicate_new_rows_using_equivalent_piece_aliases_are_ambiguous(self):
		self.source.append(dict(self.source[1], id="DUPLICATE-NEW", unit="Nos"))
		self.assertFalse(self.plan()["can_complete"])

	def test_existing_bindings_resolve_identical_source_rows(self):
		self.source.insert(1, dict(self.source[0], id="DUPLICATE"))
		self.doc["items"][0]["custom_crm_order_item_id"] = "CRM-ROW-1"
		self.doc["items"].append(frappe._dict(self.doc["items"][0], name="SOI-002", custom_crm_order_item_id="DUPLICATE"))
		self.assertTrue(self.plan()["can_complete"])

	def test_header_discount_and_new_vat_are_precise_blockers(self):
		self.doc.discount_amount = 5
		self.source[1]["vatAmount"] = 2
		plan = self.plan()
		self.assertFalse(plan["can_complete"])
		self.assertTrue(any("discount" in reason.lower() for reason in plan["blockers"]))
		self.assertTrue(any("VAT" in reason for reason in plan["blockers"]))

	def test_completion_mode_requires_fingerprint_and_rejects_header_changes(self):
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items", items=self.source)
		with self.assertRaisesRegex(frappe.ValidationError, "completion_fingerprint"):
			validate_sales_order_update_payload(payload)
		payload.update(completion_fingerprint="previewed", custom_remark="not a row completion")
		with self.assertRaisesRegex(frappe.ValidationError, "custom_remark"):
			validate_sales_order_update_payload(payload)

	def test_completion_rejects_stale_plan_before_native_write(self):
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint="stale", items=self.source)
		self.doc.name = "SO-NEW"
		with (patch.object(api, "validate_crm_api_user"),
			patch.object(api, "crm_sales_order_creation_lock", return_value=nullcontext()),
			patch.object(api, "get_sales_order_names_by_crm_order_no", return_value=["SO-NEW"]) as names,
			patch.object(frappe, "get_doc", return_value=self.doc),
			patch.object(frappe.db, "sql", return_value=[]),
			patch.object(api, "is_crm_integration_enabled", return_value=True),
			patch.object(api, "plan_missing_crm_sales_order_items", return_value=self.plan()) as planner,
			patch.object(api, "update_child_qty_rate") as native):
			with self.assertRaisesRegex(frappe.ValidationError, "重新预览"):
				update_sales_order_by_crm_order_no(payload)
			names.assert_called_once_with("CRM-001", docstatus=1)
			planner.assert_called_once_with(self.doc, self.source, historical_replay=False)
			native.assert_not_called()

	def test_completion_rechecks_crm_identity_after_parent_lock(self):
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint="previewed", items=self.source)
		self.doc.custom_crm_order_no = "CRM-OTHER"
		with (patch.object(api, "validate_crm_api_user"),
			patch.object(api, "crm_sales_order_creation_lock", return_value=nullcontext()),
			patch.object(frappe, "get_all", side_effect=lambda doctype, **kwargs: ["SO-001"] if doctype == "Sales Order" else []),
			patch.object(frappe, "get_doc", return_value=self.doc), patch.object(frappe.db, "sql", return_value=[]),
			patch.object(api, "is_crm_integration_enabled", return_value=True),
			patch.object(api, "plan_missing_crm_sales_order_items") as planner,
			patch.object(api, "update_child_qty_rate") as native):
			with self.assertRaisesRegex(frappe.ValidationError, "CRM.*变化"):
				update_sales_order_by_crm_order_no(payload)
			planner.assert_not_called()
			native.assert_not_called()

	def test_native_completion_passes_all_old_rows_unchanged_and_binds_only_new(self):
		plan = self.plan()
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint=plan["completion_fingerprint"], items=self.source)
		doc = MagicMock()
		doc.name, doc.docstatus, doc.custom_crm_order_no = "SO-001", 1, "CRM-001"
		doc.get.side_effect = lambda field, default=None: self.doc.get(field, default)
		doc.as_dict.side_effect = lambda **kwargs: deepcopy(self.doc)
		doc.items = self.doc["items"]
		before = deepcopy(self.doc["items"][0])
		def native_write(**kwargs):
			rows = frappe.parse_json(kwargs["trans_items"])
			self.assertEqual(rows[0]["docname"], "SOI-001")
			for field in ("qty", "rate", "uom", "conversion_factor", "delivery_date", "description", "custom_product", "custom_remark"):
				self.assertEqual(rows[0][field], before[field])
			self.assertEqual(len(rows), 2)
			self.doc["items"].append(frappe._dict(plan["added_items"][0], name="SOI-NEW"))
		with (patch.object(api, "validate_crm_api_user"),
			patch.object(api, "crm_sales_order_creation_lock", return_value=nullcontext()) as lock,
			patch.object(frappe, "get_all", side_effect=lambda doctype, **kwargs: ["SO-001"] if doctype == "Sales Order" else []),
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe.db, "sql", return_value=[]) as sql,
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(api, "is_crm_integration_enabled", return_value=True),
			patch.object(api, "plan_missing_crm_sales_order_items", return_value=plan),
			patch.object(api, "create_crm_log", return_value=frappe._dict(name="LOG-RECEIPT")) as receipt,
			patch.object(api, "update_child_qty_rate", side_effect=native_write) as native):
			result = update_sales_order_by_crm_order_no(payload)
			lock.assert_called_once_with("CRM-001")
			self.assertEqual(len([call for call in sql.call_args_list if "FOR UPDATE" in call.args[0]]), 3)
			native.assert_called_once()
			self.assertEqual(native.call_args.kwargs["parent_doctype"], "Sales Order")
			self.assertEqual(set_value.call_args.args[0:2], ("Sales Order Item", "SOI-NEW"))
			self.assertEqual(result["added_items"][0]["name"], "SOI-NEW")
			self.assertTrue(result["order_fingerprint"])
			self.assertTrue(receipt.called, "native completion must persist an atomic retry receipt")
			self.assertEqual(receipt.call_args.kwargs["trace_id"], payload["completion_fingerprint"])
			self.assertEqual(receipt.call_args.kwargs["request_payload"], payload)
			self.assertEqual(receipt.call_args.kwargs["response_payload"], result)
			doc.save.assert_not_called()
			self.assertEqual(self.doc["items"][0], before)

	def test_committed_completion_receipt_replays_before_current_status_checks(self):
		self.assertTrue(callable(getattr(api, "get_crm_missing_items_completion_receipt", None)))
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint="committed-preview", items=self.source)
		response = dict(status="success", name="SO-001", crm_order_no="CRM-001", docstatus=1,
			added_items=[dict(name="SOI-NEW")], order_fingerprint="committed-order-version")
		log = frappe._dict(reference_name="SO-001", request_payload=frappe.as_json(payload),
			response_payload=frappe.as_json(response))
		with (patch.object(api, "validate_crm_api_user"),
			patch.object(api, "crm_sales_order_creation_lock", return_value=nullcontext()),
			patch.object(frappe, "get_all", return_value=[log]),
			patch.object(frappe.db, "sql", return_value=[log]),
			patch.object(frappe, "get_doc") as get_doc,
			patch.object(api, "update_child_qty_rate") as native):
			self.assertEqual(update_sales_order_by_crm_order_no(payload), response)
			get_doc.assert_not_called()
			native.assert_not_called()

	def test_completion_receipt_rejects_reused_trace_with_changed_full_manifest(self):
		self.assertTrue(callable(getattr(api, "get_crm_missing_items_completion_receipt", None)))
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint="committed-preview", items=self.source)
		stored = deepcopy(payload)
		stored["items"][0]["quantity"] = 500
		log = frappe._dict(reference_name="SO-001", request_payload=frappe.as_json(stored),
			response_payload=frappe.as_json(dict(name="SO-001", crm_order_no="CRM-001")))
		with (patch.object(api, "validate_crm_api_user"),
			patch.object(api, "crm_sales_order_creation_lock", return_value=nullcontext()),
			patch.object(frappe, "get_all", return_value=[log]),
			patch.object(frappe.db, "sql", return_value=[log]),
			patch.object(api, "update_child_qty_rate") as native):
			with self.assertRaisesRegex(frappe.ValidationError, "manifest"):
				update_sales_order_by_crm_order_no(payload)
			native.assert_not_called()

	def test_readonly_completion_receipt_rpc_authenticates_without_locks_or_writes(self):
		endpoint = getattr(api, "get_sales_order_completion_receipt", None)
		self.assertTrue(callable(endpoint), "read-only timeout receipt RPC must be exposed")
		payload = dict(custom_crm_order_no="CRM-001", mode="complete_missing_items",
			completion_fingerprint="committed-preview", items=self.source)
		response = dict(name="SO-001", order_fingerprint="committed-order-version")
		for stored in (None, response):
			with (patch.object(api, "validate_crm_api_user") as auth,
				patch.object(api, "get_crm_missing_items_completion_receipt", return_value=stored) as receipt,
				patch.object(api, "crm_sales_order_creation_lock") as lock,
				patch.object(api, "create_crm_log") as log,
				patch.object(api, "update_child_qty_rate") as native):
				result = endpoint(payload)
				self.assertEqual(result, dict(status="found" if stored else "missing", receipt=stored))
				auth.assert_called_once()
				receipt.assert_called_once_with(payload)
				lock.assert_not_called()
				log.assert_not_called()
				native.assert_not_called()

	def test_historical_creation_notification_blocker_precedes_all_writes(self):
		with (patch("crm_integration.crm_integration.delivery_note.get_crm_historical_parent_creation_blockers",
			return_value=["Draft Notification Rule would send DingTalk"], create=True),
			patch.object(api, "ensure_sales_persons_exist") as people,
			patch.object(api, "create_crm_log") as log,
			patch.object(frappe, "get_doc") as factory):
			with self.assertRaisesRegex(frappe.ValidationError, "DingTalk"):
				api.create_sales_order(dict(crm_historical_replay=True, company="Test Company"))
			people.assert_not_called()
			log.assert_not_called()
			factory.assert_not_called()

	def test_normal_sales_order_creation_keeps_existing_notification_path(self):
		doc = MagicMock()
		doc.name, doc.docstatus = "SO-NEW", 1
		doc.get.return_value = "Pending Deposit Confirmation"
		with (patch("crm_integration.crm_integration.delivery_note.get_crm_historical_parent_creation_blockers", create=True) as guard,
			patch.object(api, "ensure_sales_persons_exist"),
			patch.object(api, "create_crm_log"), patch.object(api, "update_crm_log"),
			patch.object(api, "preserve_explicit_zero_rates"),
			patch.object(frappe, "get_doc", return_value=doc)):
			result = api.create_sales_order(dict(company="Test Company"))
			self.assertEqual(result["name"], "SO-NEW")
			guard.assert_not_called()
			doc.submit.assert_called_once()


class TestNativeMissingCRMOrderItems(UnitTestCase):
	"""Rollback-only native acceptance using the explicitly prepared source-adaptation QA site."""

	def setUp(self):
		if frappe.local.site != "purchase-source-adaptation-qa.localhost":
			self.skipTest("requires the isolated, prepared CRM completion QA site")
		from crm_integration.crm_integration.delivery_note import get_crm_historical_parent_creation_blockers
		self.assertEqual(get_crm_historical_parent_creation_blockers("QA Source MX"), [])
		from deeplinkerp_branding.services.purchase_repost_boundary import initialize
		initialize()
		self.point = "crm_line_fixture_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(self.point)
		self.addCleanup(lambda: frappe.db.rollback(save_point=self.point))
		token = "QA-CRM-LINE-" + frappe.generate_hash(length=8)
		self.codes = [token + "-A", token + "-B"]
		for code in self.codes:
			frappe.get_doc(dict(doctype="Item", item_code=code, item_name=code, description="Native fixture item",
				item_group="QA Source Services", stock_uom="Nos", is_stock_item=1, is_sales_item=1,
				item_defaults=[dict(company="QA Source MX", default_warehouse="Stores - QASMX")])).insert()
		self.order = frappe.get_doc(dict(doctype="Sales Order", company="QA Source MX", customer="QA CRM Outbound Customer",
			custom_crm_order_no=token, transaction_date="2026-10-15", delivery_date="2026-10-15",
			currency="MXN", conversion_rate=1, selling_price_list="QA CRM Selling MXN", set_warehouse="Stores - QASMX",
			items=[dict(item_code=self.codes[0], qty=2, rate=10, delivery_date="2026-10-15",
				custom_version="V1", description="Keep human description", custom_product="Human product")])).insert()
		self.order.submit()
		self.order.reload()
		self.source = [TestMissingCRMOrderItems.source_row(token + "-1", self.codes[0], 2, 10),
			TestMissingCRMOrderItems.source_row(token + "-2", self.codes[1], 3, 0)]
		self.plan = api.plan_missing_crm_sales_order_items(self.order, self.source)
		self.assertTrue(self.plan["can_complete"], self.plan["blockers"])
		self.payload = dict(custom_crm_order_no=token, mode="complete_missing_items", items=self.source,
			completion_fingerprint=self.plan["completion_fingerprint"])

	def test_native_append_preserves_human_row_and_timeout_retry_uses_receipt(self):
		old = self.order.items[0].as_dict()
		response = api.complete_missing_crm_sales_order_items(self.payload)
		self.order.reload()
		self.assertEqual(len(self.order.items), 2)
		self.assertEqual(self.order.items[0].name, old["name"])
		for field in ("description", "custom_product", "custom_version", "qty", "rate", "uom", "warehouse"):
			self.assertEqual(self.order.items[0].get(field), old[field])
		self.assertFalse(self.order.items[0].get("custom_crm_order_item_id"))
		new = self.order.items[1]
		self.assertEqual(new.get("custom_crm_order_item_id"), self.source[1]["id"])
		self.assertEqual(new.custom_version, "V1")
		self.assertEqual(new.rate, 0)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": ["in", self.codes]}), 0)
		self.assertEqual(frappe.db.count("CRM Integration Log", {"event": api.CRM_MISSING_ITEMS_EVENT,
			"trace_id": self.payload["completion_fingerprint"], "reference_name": self.order.name}), 1)
		# A later native state must not turn a committed-but-timed-out append into another write.
		frappe.db.set_value("Sales Order", self.order.name, "status", "Closed", update_modified=False)
		with patch.object(api, "update_child_qty_rate") as native:
			self.assertEqual(api.complete_missing_crm_sales_order_items(self.payload), response)
			native.assert_not_called()
		self.assertEqual(frappe.db.count("Sales Order Item", {"parent": self.order.name}), 2)

	def test_failed_receipt_rolls_back_actual_native_append_and_new_bindings(self):
		with patch.object(api, "create_crm_log", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "回执保存失败"):
				api.complete_missing_crm_sales_order_items(self.payload)
		self.order.reload()
		self.assertEqual(len(self.order.items), 1)
		self.assertFalse(self.order.items[0].get("custom_crm_order_item_id"))
		self.assertEqual(frappe.db.count("CRM Integration Log", {"event": api.CRM_MISSING_ITEMS_EVENT,
			"trace_id": self.payload["completion_fingerprint"]}), 0)
