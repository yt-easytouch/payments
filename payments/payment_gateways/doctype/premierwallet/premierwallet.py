import base64

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method

from payments.payment_gateways.doctype.waafipay.waafipay import create_mode_of_payment
from payments.utils import create_payment_gateway

CODE_SUCCESS = "001"
CHANNEL_ID = "104"
DEVICE_TYPE = "205"
STATUS_EXECUTED = ("Executed", "1")
STATUS_REJECTED = ("Rejected", "3")
TOKEN_TTL = 50 * 60


class AuthenticationExpired(Exception):
	pass


class Premierwallet(Document):
	supported_currencies = ("USD",)

	def on_update(self):
		gateway = "Premierwallet-" + self.gateway_name
		create_payment_gateway(gateway, settings="Premierwallet", controller=self.gateway_name)
		call_hook_method("payment_gateway_enabled", gateway=gateway, payment_channel="Phone")
		create_mode_of_payment(gateway)
		frappe.cache.delete_value(self.token_cache_key)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Premier Wallet does not support transactions in currency '{0}'"
				).format(currency)
			)

	@staticmethod
	def normalize_phone_number(account_no) -> str:
		number = "".join(character for character in str(account_no) if character.isdigit())
		if number.startswith("00"):
			number = number[2:]
		if number.startswith("252"):
			number = number[3:]
		return "252" + number.lstrip("0")

	@property
	def token_cache_key(self) -> str:
		return f"premierwallet_token:{self.gateway_name}"

	def get_headers(self, authorization: str) -> dict:
		return {
			"Authorization": authorization,
			"ChannelID": CHANNEL_ID,
			"DeviceType": DEVICE_TYPE,
			"MachineID": self.machine_id or "",
			"Content-Type": "application/json",
		}

	def _send(self, api_endpoint, payload, authorization):
		url = f"{self.endpoint.rstrip('/')}/api/{api_endpoint}"
		response = requests.post(url, json=payload, headers=self.get_headers(authorization), timeout=(5, 30))
		if response.status_code == 401:
			raise AuthenticationExpired
		response.raise_for_status()
		result = response.json()
		frappe.logger("premierwallet").info({"endpoint": api_endpoint, "response": result})
		return result

	def get_token(self) -> str:
		token = frappe.cache.get_value(self.token_cache_key)
		if token:
			return token

		credentials = f"{self.user_id}:{self.get_password('merchant_key')}"
		basic = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
		response = self._send("MerchantLogin", {}, basic)
		data = response.get("Data") or {}
		token = data.get("Token") or data.get("AccessToken") or response.get("access_token")
		if not token:
			raise frappe.AuthenticationError(self._extract_error_message(response))

		frappe.cache.set_value(self.token_cache_key, token, expires_in_sec=TOKEN_TTL)
		return token

	def _post(self, api_endpoint, payload):
		try:
			try:
				return self._send(api_endpoint, payload, "Bearer " + self.get_token())
			except AuthenticationExpired:
				frappe.cache.delete_value(self.token_cache_key)
				return self._send(api_endpoint, payload, "Bearer " + self.get_token())

		except (requests.RequestException, AuthenticationExpired, frappe.AuthenticationError) as e:
			frappe.log_error(title="Premier Wallet Request Error", message=str(e))
			return {"Response": {"Code": "-1", "Messages": str(e) or "Authentication failed"}}

		except ValueError as e:
			frappe.log_error(title="Premier Wallet Invalid Response", message=frappe.get_traceback())
			return {"Response": {"Code": "-1", "Messages": str(e)}}

	@staticmethod
	def is_success(response) -> bool:
		return (response.get("Response") or {}).get("Code") == CODE_SUCCESS

	@staticmethod
	def _extract_error_message(response):
		status = response.get("Response") or {}
		message = status.get("Messages") or "Unknown Error"
		code = status.get("Code")
		message = f"[{code}] {message}" if code is not None else message
		errors = status.get("Errors") or []
		details = "; ".join(str(error) for error in errors)
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
		"""Push a payment request to the customer's Premier Wallet app and wait for approval."""
		if self.staging == 1:
			return {
				"status": True,
				"transactionId": "PW0000000000",
				"response": {"Response": {"Code": CODE_SUCCESS}, "Data": {"Status": "Executed"}},
				"response_massage": "",
			}

		self.validate_transaction_currency(currency)

		user_id = self.normalize_phone_number(account_no)
		wallet = self.get_wallet(user_id)
		if not self.is_success(wallet):
			return self._result(wallet, request={"UserId": user_id})

		payload = {
			"CustomerWalletID": wallet["Data"]["WalletId"],
			"Amount": float(amount),
			"Remarks": str(invoice_id or ""),
			"Category": self.category or 1,
			"LoginUserName": self.merchant_id,
		}
		return self._result(self._post("PushPayment", payload), request=payload)

	def check_payment(self, transaction_id):
		"""Check a pushed payment once; callers repeat it while `pending` is true."""
		request = {"TransactionID": transaction_id, "LoginUserName": self.merchant_id}
		return self._result(self._post("GetPaymentDetails", request), request=request)

	def _result(self, response, request):
		data = response.get("Data") or {}
		status = str(data.get("Status"))
		is_paid = self.is_success(response) and status in STATUS_EXECUTED
		is_rejected = self.is_success(response) and status in STATUS_REJECTED
		is_pending = self.is_success(response) and not (is_paid or is_rejected)
		if is_paid:
			message = ""
		elif is_pending:
			message = _("Waiting for the customer to approve the payment in Premier Wallet.")
		elif is_rejected:
			message = _("The customer rejected the payment in Premier Wallet.")
		else:
			message = self._extract_error_message(response)

		return {
			"status": is_paid,
			"pending": is_pending,
			"rejected": is_rejected,
			"transactionId": data.get("TransactionId") if is_paid else None,
			"invoiceId": data.get("TransactionId") or data.get("TransactionID"),
			"response": response,
			"request": request,
			"response_massage": message,
		}

	def get_wallet(self, user_id):
		return self._post("GetUserQRDetailByUserID", {"UserId": user_id})

	def reverse_payment(self, transaction_id):
		response = self._post(
			"PushPaymentReversal", {"TransactionID": transaction_id, "LoginUserName": self.merchant_id}
		)
		is_reversed = self.is_success(response)
		return {
			"status": is_reversed,
			"response": response,
			"response_massage": "" if is_reversed else self._extract_error_message(response),
		}


@frappe.whitelist()
def pay_with_premierwallet(account_no, amount, invoice_id, gateway_name):
	settings = frappe.get_doc("Premierwallet", gateway_name)
	try:
		return settings.make_purchase(account_no, amount, invoice_id)
	except Exception:
		frappe.log_error(title="Premier Wallet Payment Failure", message=frappe.get_traceback())
		return {"status": False, "message": "Internal Server Error"}
