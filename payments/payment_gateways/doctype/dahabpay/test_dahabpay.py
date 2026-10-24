import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
import requests

from payments.payment_gateways.doctype.dahabpay.dahabpay import DahabPay

PAID = {"InvoiceStatus": "Paid", "TransactionId": "MP1.1", "invoiceId": "INV1", "StatusCode": 0}
PENDING = {"InvoiceStatus": "Pending", "TransactionId": None, "invoiceId": "INV1", "StatusCode": 0}


def make_settings(**overrides):
	settings = DahabPay.__new__(DahabPay)
	settings.__dict__.update(
		gateway_name="Test", agent_code="5555", endpoint="https://edahab.test/api/api", staging=0, **overrides
	)
	secrets = {"api_key": "key", "api_secret": "secret"}
	settings.get_password = lambda fieldname: secrets[fieldname]
	return settings


def mock_response(payload):
	response = MagicMock()
	response.json.return_value = payload
	return response


class TestDahabPay(unittest.TestCase):
	def test_generate_hash_matches_documented_example(self):
		body = '{"apiKey":"123","invoiceId":"ABC"}'
		expected = "e31f35b1b8e354de379d250f5d87c421a6bb738441d199e8ed23dddbd8cd07e4"

		self.assertEqual(DahabPay.generate_hash(body, "secretapikey"), expected)

	@patch("requests.post")
	def test_request_is_signed_over_sent_body(self, post):
		post.return_value = mock_response(PAID)

		make_settings().make_purchase("659999999", 5, "INV1")

		_, kwargs = post.call_args
		self.assertEqual(
			kwargs["params"]["hash"], hashlib.sha256((kwargs["data"] + "secret").encode()).hexdigest()
		)
		self.assertEqual(
			json.loads(kwargs["data"]),
			{
				"apiKey": "key",
				"edahabNumber": "659999999",
				"amount": 5.0,
				"agentCode": "5555",
				"currency": "USD",
			},
		)
		self.assertEqual(post.call_args[0][0], "https://edahab.test/api/api/Issueinvoice")

	@patch("requests.post")
	def test_purchase_success(self, post):
		post.return_value = mock_response(PAID)

		result = make_settings().make_purchase("659999999", 5, "INV1")

		self.assertTrue(result["status"])
		self.assertEqual(result["transactionId"], "MP1.1")
		self.assertEqual(result["request"]["apiKey"], "***")

	@patch("requests.post")
	def test_purchase_declined_insufficient_balance(self, post):
		post.return_value = mock_response(
			{
				"InvoiceStatus": "Invalid",
				"StatusCode": 5,
				"StatusDescription": "Insufficient_Customer_Balance",
			}
		)

		result = make_settings().make_purchase("659999999", 5, "INV1")

		self.assertFalse(result["status"])
		self.assertIsNone(result["transactionId"])
		self.assertIn("Insufficient_Customer_Balance", result["response_massage"])

	@patch("requests.post")
	def test_purchase_reports_validation_errors(self, post):
		post.return_value = mock_response(
			{
				"StatusCode": 3,
				"StatusDescription": "Validation_Error",
				"validationErrors": [{"Property": "amount", "ErrorMessage": "Too small"}],
			}
		)

		result = make_settings().make_purchase("659999999", 0.0001, "INV1")

		self.assertFalse(result["status"])
		self.assertIn("amount: Too small", result["response_massage"])

	@patch("requests.post")
	def test_pending_invoice_is_resolved_by_status_check(self, post):
		post.side_effect = [mock_response(PENDING), mock_response(PAID)]

		result = make_settings().make_purchase("659999999", 5, "INV1")

		self.assertTrue(result["status"])
		self.assertTrue(post.call_args[0][0].endswith("/CheckInvoiceStatus"))
		self.assertEqual(json.loads(post.call_args[1]["data"]), {"apiKey": "key", "invoiceId": "INV1"})

	@patch("requests.post")
	def test_pending_invoice_stays_unpaid(self, post):
		post.side_effect = [mock_response(PENDING), mock_response(PENDING)]

		result = make_settings().make_purchase("659999999", 5, "INV1")

		self.assertFalse(result["status"])
		self.assertEqual(result["invoiceId"], "INV1")

	@patch("frappe.log_error")
	@patch("requests.post", side_effect=requests.ConnectionError("timeout"))
	def test_network_error_returns_failure(self, post, log_error):
		result = make_settings().make_purchase("659999999", 5, "INV1")

		self.assertFalse(result["status"])
		self.assertIn("timeout", result["response_massage"])

	def test_unsupported_currency_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_settings().validate_transaction_currency("EUR")

	def test_normalize_phone_number_removes_country_prefix_252(self):
		self.assertEqual(DahabPay.normalize_phone_number("252651234567"), "651234567")

	def test_normalize_phone_number_removes_double_zero_prefix(self):
		self.assertEqual(DahabPay.normalize_phone_number("00252651234567"), "651234567")

	def test_normalize_phone_number_removes_formatting(self):
		self.assertEqual(DahabPay.normalize_phone_number("+252 65 123 4567"), "651234567")

	def test_normalize_phone_number_keeps_local_number(self):
		self.assertEqual(DahabPay.normalize_phone_number("651234567"), "651234567")

	def test_normalize_phone_number_keeps_leading_zero(self):
		self.assertEqual(DahabPay.normalize_phone_number("0651234567"), "0651234567")

	@patch("requests.post")
	def test_make_purchase_normalizes_phone_number(self, post):
		post.return_value = mock_response(PAID)

		make_settings().make_purchase("+252651234567", 5, "INV1")

		_, kwargs = post.call_args
		self.assertEqual(
			json.loads(kwargs["data"]),
			{
				"apiKey": "key",
				"edahabNumber": "651234567",
				"amount": 5.0,
				"agentCode": "5555",
				"currency": "USD",
			},
		)
