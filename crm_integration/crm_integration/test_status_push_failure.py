import json
import traceback
from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import patch

import frappe
from requests import Response
from requests.exceptions import Timeout

from crm_integration.crm_integration import sales_order as flow
from crm_integration.crm_integration.status_push_failure import redact_evidence, status_failure_evidence


class TestCRMStatusFailureRollback(TestCase):
	def setUp(self):
		self.order = frappe._dict(
			name="SO-LOG-TEST", custom_crm_order_no="CRM-LOG-TEST", company="TEST",
			custom_crm_approval_round=0, custom_process_status=flow.DEPOSIT_CONFIRMATION_PROCESSING,
			custom_remark="private remark must not be logged",
		)
		self.secret = "test-api-key-for-log"
		self.url = "https://user:privatepass@crm.invalid/status?access_token=query-secret"
		self.logs = []
		self.operations = []
		self.enterContext(patch.object(flow, "is_crm_integration_enabled", return_value=True))
		self.enterContext(patch.object(flow, "get_crm_status_api_url", return_value=self.url))
		self.enterContext(patch.object(flow, "get_crm_status_api_key", return_value=self.secret))
		self.enterContext(patch.object(flow, "get_crm_status_api_verify_ssl", return_value=True))
		self.enterContext(patch.object(flow, "create_crm_log", side_effect=self.create_log))
		self.enterContext(patch.object(flow, "update_crm_log"))
		self.enterContext(patch.object(frappe, "log_error"))
		self.enterContext(patch.object(frappe, "get_doc", return_value=self.order))
		self.enterContext(patch.object(frappe.db, "get_value"))
		self.enterContext(patch.object(flow, "crm_sales_order_creation_lock", return_value=nullcontext()))
		self.rollback = self.enterContext(patch.object(frappe.db, "rollback", side_effect=self.rollback_logs))
		self.commit = self.enterContext(patch.object(frappe.db, "commit", side_effect=lambda: self.operations.append("commit")))
		self.restore = self.enterContext(patch.object(flow, "restore_deposit_confirmation_after_failure"))
		self.audit = self.enterContext(patch("crm_integration.crm_integration.finance_release.update_finance_audit"))

	def create_log(self, **values):
		self.logs.append(values)
		self.operations.append(values["event"])
		return frappe._dict(name=f"TEST-LOG-{len(self.logs)}")

	def rollback_logs(self):
		self.logs.clear()
		self.operations.append("rollback")

	def response(self, status=409, body=None):
		response = Response()
		response.status_code = status
		response.url = self.url
		response.headers["Content-Type"] = "application/json"
		response._content = json.dumps(body or {"code": "ROUND_CONFLICT", "message": "审批轮次不一致"}).encode()
		return response

	def run_job(self, failed_status, responses=None, error=None):
		def sync(order):
			if failed_status == flow.CRM_STATUS_IN_PRODUCTION:
				flow.push_sales_order_status_to_crm(order, flow.CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION)
			flow.push_sales_order_status_to_crm(order, failed_status)

		with patch.object(flow, "run_confirm_deposit_sync", side_effect=sync), patch.object(
			flow.requests, "post", side_effect=error or responses,
		), self.assertRaises(frappe.ValidationError) as raised:
			flow.confirm_deposit_and_push_to_mes_job(self.order.name, audit_log="TEST-AUDIT")
		self.rollback.assert_called_once_with()
		self.commit.assert_called_once_with()
		self.restore.assert_called_once_with(self.order.name)
		self.audit.assert_called_once_with("TEST-AUDIT", self.order.name, "Failed", flow.PENDING_DEPOSIT_CONFIRMATION)
		self.assertEqual(self.operations[-4:], ["rollback", flow.get_crm_status_event(failed_status), "Confirm Deposit External Sync", "commit"])
		self.assertEqual(len(self.logs), 2)
		for log in self.logs:
			self.assertEqual(log["status"], "Failed")
			self.assertEqual(log["external_status"], failed_status)
			self.assertEqual(log["trace_id"], flow.make_crm_trace_id(self.order.name, failed_status))
			self.assertEqual(log["request_url"], "https://crm.invalid/status")
		for secret in (self.secret, "privatepass", "query-secret", "private remark"):
			self.assertNotIn(secret, str(self.logs))
			self.assertNotIn(secret, "".join(traceback.format_exception(raised.exception)))
		return raised.exception

	def test_409_recreates_http_log_and_enriches_outer_log_after_rollback(self):
		body = {"errorCode": "ROUND_CONFLICT", "message": "审批轮次不一致", "detail": "invalid query-secret",
			"token": self.secret,
			"data": {"password": "privatepass", "api_key": self.secret}}
		self.run_job(flow.CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION, [self.response(body=body)])
		for log in self.logs:
			self.assertEqual(log["http_status_code"], 409)
			self.assertEqual(log["response_payload"]["errorCode"], "ROUND_CONFLICT")
			self.assertEqual(log["response_payload"]["message"], "审批轮次不一致")
		self.assertEqual(self.logs[0]["request_payload"]["crmApprovalRound"], 0)
		self.assertEqual(self.logs[1]["request_payload"]["sales_order"], self.order.name)
		self.assertEqual(self.logs[1]["request_payload"]["crm_status_request"], self.logs[0]["request_payload"])

	def test_e3_failure_retains_the_failing_event_instead_of_e2_success(self):
		self.run_job(flow.CRM_STATUS_IN_PRODUCTION, [self.response(200, {"success": True}), self.response()])
		self.assertEqual(self.logs[0]["event"], "In Production")

	def test_timeout_without_response_records_zero_and_exception_type(self):
		self.run_job(flow.CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION,
			error=Timeout(f"timeout for {self.url}; X-API-Key={self.secret}"))
		for log in self.logs:
			self.assertEqual(log["http_status_code"], 0)
			self.assertIsNone(log["response_payload"])
			self.assertIn("Timeout", log["error_message"])

	def test_non_json_failure_response_redacts_the_entire_quoted_credential(self):
		response = self.response()
		response.headers["Content-Type"] = "text/plain"
		response._content = b'password="sample private phrase"; ROUND_CONFLICT'
		self.run_job(flow.CRM_STATUS_CONFIRMED_DEPOSIT_PUSH_PRODUCTION, [response])
		for log in self.logs:
			self.assertIn("ROUND_CONFLICT", log["response_payload"])
			self.assertNotIn("private phrase", log["response_payload"])

	def test_success_does_not_commit_or_roll_back_business_transaction(self):
		with patch.object(flow.requests, "post", return_value=self.response(200, {"success": True})) as post:
			self.assertEqual(flow.push_sales_order_status_to_crm(self.order, flow.CRM_STATUS_IN_PRODUCTION), {"success": True})
		self.commit.assert_not_called()
		self.rollback.assert_not_called()
		self.assertEqual(post.call_args.kwargs["json"]["remark"], self.order.custom_remark)
		self.assertNotIn("remark", self.logs[0]["request_payload"])


class TestCRMStatusEvidenceRedaction(TestCase):
	def test_quoted_credentials_with_spaces_and_escapes_are_fully_redacted(self):
		for value in (
			'{"password": "sample private phrase"}',
			"X-API-Key: 'sample private phrase'",
			r'''password="sample \"private\" phrase"''',
			r'''password='sample \'private\' phrase' ''',
			'authorization="Bearer sample private phrase"',
			'password="sample private phrase',
			'password="sample private phrase' + "\\",
		):
			with self.subTest(value=value):
				result = redact_evidence(value)
				self.assertIn("[REDACTED]", result)
				for part in ("sample", "private", "phrase"):
					self.assertNotIn(part, result)

	def test_multiple_quoted_credentials_do_not_consume_business_errors(self):
		result = redact_evidence('password="first private phrase"; api_key=\'second private phrase\'; errorCode=ROUND_CONFLICT')
		self.assertEqual(result.count("[REDACTED]"), 2)
		self.assertIn("errorCode=ROUND_CONFLICT", result)
		self.assertNotIn("private phrase", result)

	def test_large_data_keeps_its_key_and_prioritizes_error_summary(self):
		result = redact_evidence({"data": ["x" * 4000] * 8,
			"errorCode": "ROUND_CONFLICT", "message": "审批轮次不一致", "traceId": "crm-trace"})
		self.assertEqual(result["errorCode"], "ROUND_CONFLICT")
		self.assertEqual(result["message"], "审批轮次不一致")
		self.assertEqual(result["traceId"], "crm-trace")
		self.assertIn("data", result)
		self.assertIn("[truncated]", result["data"])
		self.assertNotIn("[truncated]", result)

	def test_long_error_list_keeps_its_field_name(self):
		result = redact_evidence({"errors": [{"code": str(i)} for i in range(70)]})
		self.assertIn("errors", result)
		self.assertIn("[truncated]", result["errors"])
		self.assertNotIn("[truncated]", result)

	def test_error_summary_after_sixty_four_fields_is_still_retained(self):
		result = redact_evidence({**{f"field{i}": "x" for i in range(100)},
			"errorCode": "ROUND_CONFLICT", "message": "审批轮次不一致"})
		self.assertEqual(result["errorCode"], "ROUND_CONFLICT")
		self.assertEqual(result["message"], "审批轮次不一致")
		self.assertTrue(result["_truncated"])

	def test_nested_fields_and_non_json_text_are_redacted(self):
		result = redact_evidence({"message": "授权失败", "errors": [{"Authorization": "Bearer private", "code": "ROUND_CONFLICT"}],
			"body": "https://user:password@crm.invalid/path?token=private authorization=private; Bearer private"})
		self.assertEqual(result["errors"][0]["code"], "ROUND_CONFLICT")
		self.assertNotIn("private", str(result))
		self.assertNotIn("password", result["body"])

	def test_oversized_response_cannot_truncate_required_log_fields(self):
		result = status_failure_evidence(order_name="SO-TEST", event="In Production", url="https://crm.invalid/status",
			payload={"traceId": "test-trace", "externalStatus": "IN_PRODUCTION"},
			response_payload={str(i): ["x" * 20000] * 100 for i in range(100)},
			response=None, error=Timeout("timeout"), api_key="test-only")
		self.assertEqual(result["trace_id"], "test-trace")
		self.assertEqual(result["external_status"], "IN_PRODUCTION")
		self.assertIn("Timeout", result["error_message"])
		self.assertLess(len(json.dumps(result)), 30000)
