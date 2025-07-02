import frappe
import uuid
import time
import requests
from frappe.model.document import Document


class WaafiPay(Document):
	def _get_endpoint(self):
		return (
			"http://sandbox.waafipay.net/asm"
			if self.staging == 1 else self.endpoint
		)

	def _build_request(self, service_name,source):
		return {
			"schemaVersion": "1.0",
			"requestId": str(uuid.uuid4()),
			"timestamp": str(int(time.time())),
			"channelName": source,
			"serviceName": service_name
		}

	def _post(self, payload):
		try:
			headers = {"Content-Type": "application/json"}
			response = requests.post(
				self._get_endpoint(),
				json=payload,
				headers=headers,
				timeout=30
			)
			response.raise_for_status()
			return response.json()
		except requests.RequestException as e:
			return {
				"status": False,
				"message": str(e),
				"payload": payload
			}

	def make_purchase(self, account_no=None, amount=None, invoice_id=None,source="WEB"):
		"""
		Send payment request to WaafiPay.
		"""
		data = self._build_request("API_PURCHASE",source)
		data["serviceParams"] = {
			"merchantUid": self.merchant_id,
			"apiUserId": self.user_id,
			"apiKey": self.merchant_key,
			"paymentMethod": "MWALLET_ACCOUNT",
			"payerInfo": {
				"accountNo": account_no
			},
			"transactionInfo": {
				"invoiceId": str(invoice_id),
				"amount": f"{float(amount):.2f}",
				"currency": "USD",
			}
		}
		return self._post(data)

	def cancel_purchase(self, transaction_id, description="Cancel request"):
		"""
		Cancel a previous payment.
		"""
		data = self._build_request("API_CANCELPURCHASE")
		data["serviceParams"] = {
			"merchantUid": self.merchant_id,
			"apiUserId": self.user_id,
			"apiKey": self.merchant_key,
			"transactionId": transaction_id,
			"description": description
		}
		return self._post(data)

	def check_transaction_status(self, reference_id):
		"""
		Optional: Query the transaction status using API_TRANSACTIONSTATUS (not always available).
		"""
		data = self._build_request("API_TRANSACTIONSTATUS")
		data["serviceParams"] = {
			"merchantUid": self.merchant_id,
			"apiUserId": self.user_id,
			"apiKey": self.merchant_key,
			"referenceId": reference_id
		}
		return self._post(data)
