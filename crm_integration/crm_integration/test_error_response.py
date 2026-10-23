import unittest
from requests import Response
from crm_integration.crm_integration.sales_order import parse_crm_response


class TestErrorResponse(unittest.TestCase):
    def test_http_errors_keep_the_counterparty_response_body(self):
        for code in (400, 401, 409, 500):
            response = Response()
            response.status_code = code
            response.headers['content-type'] = 'application/json; charset=utf-8'
            response._content = b'{"success":false,"errorCode":"ORDER_NOT_FOUND"}'
            with self.subTest(status=code):
                self.assertFalse(response)
                self.assertEqual(parse_crm_response(response)['errorCode'], 'ORDER_NOT_FOUND')

    def test_no_response_remains_none(self):
        self.assertIsNone(parse_crm_response(None))
