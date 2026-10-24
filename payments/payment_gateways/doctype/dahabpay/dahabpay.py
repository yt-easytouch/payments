import hashlib
import json

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method

from payments.payment_gateways.doctype.waafipay.waafipay import create_mode_of_payment
from payments.utils import create_payment_gateway

STATUS_PAID = "Paid"
STATUS_PENDING = "Pending"


class DahabPay(Document):
	supported_currencies = ("USD", "SLSH")

	def on_update(self):
		gateway = "DahabPay-" + self.gateway_name
		create_payment_gateway(gateway, settings="DahabPay", controller=self.gateway_name)
		call_hook_method("payment_gateway_enabled", gateway=gateway, payment_channel="Phone")
		create_mode_of_payment(gateway)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. DahabPay does not support transactions in currency '{0}'"
				).format(currency)
			)

	@staticmethod
	def generate_hash(body: str, api_secret: str) -> str:
		return hashlib.sha256((body + api_secret).encode("utf-8")).hexdigest()

	@staticmethod
	def normalize_phone_number(account_no) -> str:
		number = "".join(character for character in str(account_no) if character.isdigit())
		if number.startswith("00"):
			number = number[2:]
		if number.startswith("252"):
			number = number[3:]
		return number

	def _post(self, api_endpoint, payload):
		body = json.dumps(payload, separators=(",", ":"))
		url = f"{self.endpoint.rstrip('/')}/{api_endpoint}"
		params = {"hash": self.generate_hash(body, self.get_password("api_secret"))}

		try:
			response = requests.post(
				url,
				data=body,
				params=params,
				headers={"Content-Type": "application/json"},
				timeout=(5, 30),
			)
			response.raise_for_status()
			res = response.json()
			frappe.logger("dahabpay").info({"endpoint": api_endpoint, "response": res})
			return res

		except requests.RequestException as e:
			frappe.log_error(title="DahabPay Request Error", message=str(e))
			return {"StatusCode": -1, "StatusDescription": str(e)}

		except ValueError as e:
			frappe.log_error(title="DahabPay Invalid Response", message=frappe.get_traceback())
			return {"StatusCode": -1, "StatusDescription": str(e)}

	@staticmethod
	def _extract_error_message(response):
		lowered = {str(key).lower(): value for key, value in response.items()}
		errors = lowered.get("validationerrors") or lowered.get("errors") or []
		if isinstance(errors, dict):
			errors = [{"Property": key, "ErrorMessage": value} for key, value in errors.items()]
		details = "; ".join(
			f"{error.get('Property') or error.get('property')}: {error.get('ErrorMessage') or error.get('errorMessage')}"
			for error in errors
			if isinstance(error, dict)
		)
		message = response.get("StatusDescription") or response.get("InvoiceStatus") or "Unknown Error"
		code = response.get("StatusCode")
		message = f"[{code}] {message}" if code is not None else message
		return f"{message} ({details})" if details else message

	def request_for_payment(self, **kwargs):
		"""Entry point for Easytouch Payment Request (Phone channel)."""
		currency = kwargs.get("currency") or "USD"
		self.validate_transaction_currency(currency)

		return self.make_purchase(
			account_no=kwargs.get("phone_number"),
			amount=kwargs.get("request_amount"),
			invoice_id=kwargs.get("payment_reference") or kwargs.get("reference_docname"),
			currency=currency,
		)

	def make_purchase(self, account_no, amount, invoice_id, currency="USD", **kwargs):
		"""Issue an eDahab invoice; the customer approves it on their phone."""
		if self.staging == 1:
			return {
				"status": True,
				"transactionId": "MP000000.0000",
				"response": {"InvoiceStatus": STATUS_PAID, "StatusCode": 0, "StatusDescription": "Success"},
				"response_massage": "",
			}

		self.validate_transaction_currency(currency)

		payload = {
			"apiKey": self.get_password("api_key"),
			"edahabNumber": self.normalize_phone_number(account_no),
			"amount": float(amount),
			"agentCode": self.agent_code,
			"currency": currency,
		}
		response = self._post("Issueinvoice", payload)

		if response.get("StatusCode") == 0 and response.get("InvoiceStatus") == STATUS_PENDING:
			response = self.check_status(response.get("invoiceId"))

		is_paid = response.get("StatusCode") == 0 and response.get("InvoiceStatus") == STATUS_PAID
		response_message = "" if is_paid else self._extract_error_message(response)

		safe_request = {**payload, "apiKey": "***"}

		return {
			"status": is_paid,
			"transactionId": response.get("TransactionId") if is_paid else None,
			"invoiceId": response.get("invoiceId"),
			"response": response,
			"request": safe_request,
			"response_massage": response_message,
		}

	def check_status(self, invoice_id):
		response = self._post(
			"CheckInvoiceStatus", {"apiKey": self.get_password("api_key"), "invoiceId": invoice_id}
		)
		frappe.logger("dahabpay").info(
			{"action": "check_status", "invoice_id": invoice_id, "response": response}
		)
		return response


@frappe.whitelist()
def pay_with_dahab(account_no, amount, invoice_id, gateway_name):
	settings = frappe.get_doc("DahabPay", gateway_name)
	try:
		return settings.make_purchase(account_no, amount, invoice_id)
	except Exception:
		frappe.log_error(title="DahabPay Payment Failure", message=frappe.get_traceback())
		return {"status": False, "message": "Internal Server Error"}
