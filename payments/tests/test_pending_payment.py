import unittest
from unittest.mock import MagicMock, patch

from payments import api

PENDING = {"status": False, "pending": True, "invoiceId": "TX1", "response_massage": "Waiting"}


def make_doc():
	doc = MagicMock(doctype="Sales Invoice")
	doc.name = "SINV-1"
	return doc


@patch("payments.api.frappe.get_value", MagicMock(return_value="Gateway Account"))
class TestPendingPayment(unittest.TestCase):
	@patch("payments.api.record_gateway_payment")
	@patch("payments.api.call_payment_gateway", return_value=PENDING)
	def test_pending_charge_is_returned_without_booking(self, call_gateway, record):
		payload = {"payment_number": "615000000", "payments": [{"mode_of_payment": "Premier", "amount": 5}]}

		result = api.process_payment(make_doc(), payload)

		self.assertEqual(
			result,
			{
				"status": False,
				"pending": True,
				"transaction_id": "TX1",
				"mode_of_payment": "Premier",
				"amount": 5.0,
				"message": "Waiting",
			},
		)
		record.assert_not_called()

	@patch("payments.api.frappe.get_doc")
	def test_call_gateway_keeps_pending_out_of_failure_path(self, get_doc):
		get_doc.return_value.make_purchase.return_value = dict(PENDING)

		result = api.call_payment_gateway("Premier", 5, "SINV-1", "615000000")

		self.assertTrue(result["pending"])
		self.assertIsNone(result["_fault_type"])
