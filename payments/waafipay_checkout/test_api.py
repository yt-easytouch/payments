from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase, UnitTestCase

from payments.waafipay_checkout import api, api_public, client


class Response:
	def __init__(self, body):
		self.body, self.status_code = body, 200

	def json(self):
		return self.body


def approved(reference, amount="12.50", transaction="T-1"):
	params = {"state": "APPROVED", "referenceId": reference, "transactionId": transaction, "txAmount": amount}
	return {"responseCode": "2001", "responseMsg": "RCS_SUCCESS", "params": params}


NOT_FOUND = {"responseCode": "5310", "responseMsg": "Transaction not found", "params": {}}


def fake_post(lookup):
	def post(url, json=None, timeout=None, allow_redirects=True):
		params = json["serviceParams"]
		reference = params.get("referenceId") or params.get("transactionInfo", {}).get("referenceId")
		if json["serviceName"] == "HPP_PURCHASE":
			return Response({"responseCode": "2001", "params": {"hppUrl": "https://hpp.example/s"}})
		answer = lookup(reference)
		if isinstance(answer, Exception):
			raise answer
		return Response(answer)

	return post


class TestHelpers(UnitTestCase):
	def test_return_path_is_same_site_only(self):
		for path in ("/", "/a/b?c=d&e=f", "/dashboard/billing?payment=abc"):
			self.assertEqual(api.safe_return_path(path), path)
		for path in ("", "a", "//x.example", "/\\x.example", "/ x", "/\n/x", "https://x.example", 5, None):
			self.assertEqual(api.safe_return_path(path), "/")

	def test_phone_normalising_and_masking(self):
		self.assertEqual(client.wallet_phone("+252 61 111 1111"), "252611111111")
		self.assertIsNone(client.wallet_phone("+252 51 111 1111"))
		self.assertEqual(client.mask_phone("252611111111"), "25261*****11")
		self.assertNotIn("252611111111", str(client.scrub({"accountNo": "252611111111", "apiKey": "k"})))


class TestTransactions(IntegrationTestCase):
	def setUp(self):
		frappe.db.savepoint("payments_case")
		settings = frappe.get_single("WaafiPay Settings")
		settings.update(
			{
				"enabled": 1,
				"sandbox": 1,
				"merchant_uid": "M-FAKE-0001",
				"api_user_id": "fake-user",
				"api_key": "fake-api-key-not-real",
				"store_id": "fake-store",
				"hpp_key": "fake-hpp-key-not-real",
			}
		)
		settings.save(ignore_permissions=True)
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "payments test"}).insert(
			ignore_permissions=True
		)

	def tearDown(self):
		frappe.db.rollback(save_point="payments_case")

	def start_card(self, return_path="/thanks"):
		with patch.object(client.requests, "post", fake_post(lambda ref: NOT_FOUND)):
			return api.start("ToDo", self.todo.name, 1250, "USD", "card", return_path=return_path)

	def test_card_settles_once_after_reverification(self):
		tx = self.start_card()
		self.assertEqual(
			(tx.status, tx.redirect_url, tx.return_path), ("pending", "https://hpp.example/s", "/thanks")
		)
		with patch.object(client.requests, "post", fake_post(lambda ref: approved(ref))):
			self.assertEqual(api.verify(tx.name).status, "paid")
			api.verify(tx.name)
		row = api.get(tx.name)
		self.assertEqual((row.provider_ref, row.needs_review), ("T-1", 0))
		self.assertRaises(
			frappe.ValidationError, frappe.delete_doc, api.DOCTYPE, tx.name, ignore_permissions=True
		)

	def test_start_refuses_bad_input(self):
		for kwargs, code in (
			({"method": "paypal"}, "invalid_method"),
			({"currency": "EUR"}, "invalid_currency"),
			({"method": "evc_plus", "phone": "123"}, "invalid_phone"),
			({"amount_cents": 0}, "invalid_amount"),
			({"reference_name": "missing"}, "invalid_reference"),
		):
			args = {
				"amount_cents": 1250,
				"currency": "USD",
				"method": "card",
				"reference_name": self.todo.name,
			}
			args.update(kwargs)
			with self.subTest(code=code), self.assertRaises(api.PaymentError) as raised:
				api.start(
					"ToDo",
					args["reference_name"],
					args["amount_cents"],
					args["currency"],
					args["method"],
					phone=args.get("phone"),
				)
			self.assertEqual(raised.exception.code, code)

	def test_expiry_from_the_wrong_state_is_a_no_op(self):
		tx = self.start_card()
		frappe.db.set_value(api.DOCTYPE, tx.name, "status", "processing")
		self.assertEqual(
			api._finish(tx.name, "expired", api.NOT_SENT, expect=("pending",)).status, "processing"
		)
		api._finish(tx.name, "expired", api.EXPIRED, expect=("processing",))
		row = api.get(tx.name)
		self.assertEqual((row.status, row.needs_review), ("expired", 1))
		api.verify(tx.name, evidence=client._parse(approved(row.reference_id, transaction="T-LATE")))
		row = api.get(tx.name)
		self.assertEqual((row.status, row.provider_ref), ("expired", "T-LATE"))
		self.assertIn("T-LATE", row.review_note)

	def test_return_page_redirects_to_the_stored_path_only(self):
		tx = self.start_card(return_path="https://evil.example/")
		self.assertEqual(tx.return_path, "/")
		with patch.object(client.requests, "post", fake_post(lambda ref: requests.ConnectionError())):
			response = api_public.waafipay_return(ref=tx.reference_id)
		self.assertEqual(response.status_code, 200)
		self.assertIn('content="0;url=/"', response.get_data(as_text=True))
