import base64
import unittest
from unittest.mock import MagicMock, patch

import frappe
import requests

from payments.payment_gateways.doctype.premierwallet.premierwallet import Premierwallet

OK = {"Code": "001", "Messages": "Transaction Successful.", "Errors": []}
LOGIN = {"Response": OK, "Data": {"Token": "tok"}}
WALLET = {"Response": OK, "Data": {"WalletId": "00252615000000", "Name": "TEST"}}
PUSHED = {"Response": OK, "Data": {"TransactionID": "TX1"}}


def details(status):
	return {"Response": OK, "Data": {"TransactionId": "TX1", "Status": status}}


def make_settings(**overrides):
	settings = Premierwallet.__new__(Premierwallet)
	settings.__dict__.update(
		gateway_name="Test",
		user_id="user",
		merchant_id="420588",
		machine_id="M1",
		category=1,
		endpoint="https://premier.test/",
		staging=0,
		**overrides,
	)
	settings.get_password = lambda fieldname: {"merchant_key": "pass"}[fieldname]
	return settings


def mock_response(payload, status_code=200):
	response = MagicMock(status_code=status_code)
	response.json.return_value = payload
	return response


class FakeCache:
	def __init__(self):
		self.values = {}

	def get_value(self, key):
		return self.values.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.values[key] = value

	def delete_value(self, key):
		self.values.pop(key, None)


@patch("frappe.log_error", MagicMock())
@patch("frappe.logger", MagicMock())
class TestPremierwallet(unittest.TestCase):
	def setUp(self):
		patcher = patch.object(frappe, "cache", FakeCache(), create=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def endpoints(self, post):
		return [call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list]

	@patch("requests.post")
	def test_push_returns_pending_with_transaction_id(self, post):
		post.side_effect = [mock_response(r) for r in (LOGIN, WALLET, PUSHED)]

		result = make_settings().make_purchase("615000000", 5, "SINV-1")

		self.assertFalse(result["status"])
		self.assertTrue(result["pending"])
		self.assertEqual(result["invoiceId"], "TX1")
		self.assertEqual(self.endpoints(post), ["MerchantLogin", "GetUserQRDetailByUserID", "PushPayment"])

	@patch("requests.post")
	def test_login_uses_basic_auth_and_partner_headers(self, post):
		post.side_effect = [mock_response(r) for r in (LOGIN, WALLET, PUSHED)]

		make_settings().make_purchase("615000000", 5, "SINV-1")

		login_headers = post.call_args_list[0].kwargs["headers"]
		self.assertEqual(login_headers["Authorization"], "Basic " + base64.b64encode(b"user:pass").decode())
		self.assertEqual(
			(login_headers["ChannelID"], login_headers["DeviceType"], login_headers["MachineID"]),
			("104", "205", "M1"),
		)
		self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer tok")

	@patch("requests.post")
	def test_push_payment_payload(self, post):
		post.side_effect = [mock_response(r) for r in (LOGIN, WALLET, PUSHED)]

		make_settings().make_purchase("+252 61 500 0000", 5, "SINV-1")

		self.assertEqual(post.call_args_list[1].kwargs["json"], {"UserId": "252615000000"})
		self.assertEqual(
			post.call_args_list[2].kwargs["json"],
			{
				"CustomerWalletID": "00252615000000",
				"Amount": 5.0,
				"Remarks": "SINV-1",
				"Category": 1,
				"LoginUserName": "420588",
			},
		)

	@patch("requests.post")
	def test_token_is_cached_between_calls(self, post):
		post.side_effect = [mock_response(r) for r in (LOGIN, WALLET, WALLET)]
		settings = make_settings()

		settings.get_wallet("252615000000")
		settings.get_wallet("252615000000")

		self.assertEqual(self.endpoints(post).count("MerchantLogin"), 1)

	@patch("requests.post")
	def test_expired_token_logs_in_again(self, post):
		frappe.cache.set_value("premierwallet_token:Test", "old")
		post.side_effect = [mock_response({}, 401), mock_response(LOGIN), mock_response(WALLET)]

		response = make_settings().get_wallet("252615000000")

		self.assertEqual(response, WALLET)
		self.assertEqual(post.call_args_list[2].kwargs["headers"]["Authorization"], "Bearer tok")

	@patch("requests.post")
	def test_check_payment_executed_is_paid(self, post):
		post.side_effect = [mock_response(LOGIN), mock_response(details("Executed"))]

		result = make_settings().check_payment("TX1")

		self.assertTrue(result["status"])
		self.assertFalse(result["pending"])
		self.assertEqual(result["transactionId"], "TX1")
		self.assertEqual(post.call_args.kwargs["json"], {"TransactionID": "TX1", "LoginUserName": "420588"})

	@patch("requests.post")
	def test_check_payment_new_or_approval_required_stays_pending(self, post):
		post.side_effect = [
			mock_response(r) for r in (LOGIN, details("New"), details("ApprovalRequired"), details(0))
		]
		settings = make_settings()

		for _ in range(3):
			result = settings.check_payment("TX1")
			self.assertFalse(result["status"])
			self.assertTrue(result["pending"])

	@patch("requests.post")
	def test_check_payment_rejected_is_final_failure(self, post):
		post.side_effect = [mock_response(LOGIN), mock_response(details("Rejected"))]

		result = make_settings().check_payment("TX1")

		self.assertFalse(result["status"])
		self.assertFalse(result["pending"])
		self.assertTrue(result["rejected"])
		self.assertIsNone(result["transactionId"])
		self.assertIn("rejected", result["response_massage"])

	@patch("requests.post")
	def test_check_payment_api_error_is_not_a_rejection(self, post):
		error = {"Response": {"Code": "005", "Messages": "Not found", "Errors": []}}
		post.side_effect = [mock_response(LOGIN), mock_response(error)]

		result = make_settings().check_payment("TX1")

		self.assertFalse(result["status"])
		self.assertFalse(result["pending"])
		self.assertFalse(result["rejected"])
		self.assertEqual(result["response_massage"], "[005] Not found")

	@patch("requests.post")
	def test_unknown_wallet_returns_error_message(self, post):
		error = {"Response": {"Code": "002", "Messages": "User not found", "Errors": ["UserId"]}}
		post.side_effect = [mock_response(LOGIN), mock_response(error)]

		result = make_settings().make_purchase("615000000", 5, "SINV-1")

		self.assertFalse(result["status"])
		self.assertEqual(result["response_massage"], "[002] User not found (UserId)")
		self.assertNotIn("PushPayment", self.endpoints(post))

	@patch("requests.post", side_effect=requests.ConnectionError("timeout"))
	def test_network_error_returns_failure(self, post):
		result = make_settings().make_purchase("615000000", 5, "SINV-1")

		self.assertFalse(result["status"])
		self.assertIn("timeout", result["response_massage"])

	@patch("requests.post")
	def test_reverse_payment(self, post):
		post.side_effect = [mock_response(LOGIN), mock_response({"Response": OK})]

		result = make_settings().reverse_payment("TX1")

		self.assertTrue(result["status"])
		self.assertEqual(post.call_args.kwargs["json"], {"TransactionID": "TX1", "LoginUserName": "420588"})
		self.assertTrue(post.call_args.args[0].endswith("/api/PushPaymentReversal"))

	@patch("frappe.only_for", MagicMock())
	@patch("requests.post")
	def test_test_login_ignores_cached_token(self, post):
		frappe.cache.set_value("premierwallet_token:Test", "old")
		post.return_value = mock_response(LOGIN)

		result = make_settings().test_login()

		self.assertTrue(result["status"])
		self.assertEqual(self.endpoints(post), ["MerchantLogin"])
		self.assertEqual(frappe.cache.get_value("premierwallet_token:Test"), "tok")

	@patch("frappe.only_for", MagicMock())
	@patch("requests.post")
	def test_test_login_reports_rejected_credentials(self, post):
		post.return_value = mock_response({}, 401)

		result = make_settings().test_login()

		self.assertFalse(result["status"])
		self.assertIn("401", result["message"])

	@patch("frappe.only_for", MagicMock())
	@patch("requests.post")
	def test_test_login_reports_missing_token(self, post):
		post.return_value = mock_response({"Response": {"Code": "002", "Messages": "Invalid user"}})

		result = make_settings().test_login()

		self.assertFalse(result["status"])
		self.assertIn("Invalid user", result["message"])

	def test_unsupported_currency_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			make_settings().validate_transaction_currency("EUR")

	def test_normalize_phone_number_adds_country_code(self):
		for number in ("615000000", "0615000000", "252615000000", "00252615000000", "+252 61 500 0000"):
			self.assertEqual(Premierwallet.normalize_phone_number(number), "252615000000")
