import frappe
import json
import uuid
import time
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method
from payments.utils import erpnext_app_import_guard

class WaafiPay(Document):
    
    supported_currencies = ("USD")

    def _get_endpoint(self):
        return "http://sandbox.waafipay.net/asm" if self.staging == 1 else self.endpoint

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Stripe does not support transactions in currency '{0}'"
                ).format(currency)
            )
    def _build_request(self, service_name, source="WEB"):
        return {
            "schemaVersion": "1.0",
            "requestId": str(uuid.uuid4()),
            "timestamp": str(int(time.time())),
            "channelName": source,
            "serviceName": service_name
        }

    def _post(self, payload):
        """Send POST request and handle errors with logging."""
        try:
            endpoint = self._get_endpoint()
            headers = {"Content-Type": "application/json"}
            response = requests.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            res = response.json()

            frappe.logger("waafipay").info({
                "status": "OK",
                "endpoint": endpoint,
                "payload": payload,
                "response": res
            })

            return res

        except requests.RequestException as e:
            error_data = {
                "status": False,
                "message": str(e),
                "payload": payload
            }
            frappe.log_error(
                title="WaafiPay Request Error",
                message=frappe.as_json(error_data)
            )
            return error_data

        except Exception as e:
            frappe.log_error(
                title="WaafiPay Unexpected Error",
                message=frappe.get_traceback()
            )
            return {"status": False, "message": str(e), "payload": payload}

    def request_for_payment(self, **kwargs):
        """
        Entry point for Easytouch Payment Request (Phone channel).
        Expects kwargs from PaymentRequest.request_phone_payment.
        """
        phone_number = kwargs.get("phone_number")
        amount = kwargs.get("request_amount")
        currency = kwargs.get("currency") or "USD"
        reference = kwargs.get("payment_reference") or kwargs.get("reference_docname")
        reference_doctype = kwargs.get("reference_doctype")
        reference_docname = kwargs.get("reference_docname")
        metadata = {
            "reference": reference,
            "reference_doctype": reference_doctype,
            "reference_docname": reference_docname,
        }

        self.validate_transaction_currency(currency)

        provider = kwargs.get("payment_method")
        if not provider and reference_doctype == "Payment Request" and reference_docname:
            try:
                pr = frappe.get_doc("Payment Request", reference_docname)
                if pr.message:
                    provider = json.loads(pr.message or "{}").get("provider")
            except Exception:
                provider = None

        # Provider is optional; make_purchase defaults to waafipay
        return self.make_purchase(
            account_no=phone_number,
            amount=amount,
            invoice_id=reference,
            reference_doctype=reference_doctype,
            reference_docname=reference_docname,
            provider=provider,
            payment_request=reference_docname,
            metadata=metadata,
        )

    @staticmethod
    def _extract_error_message(response):
        """Best available human message, falling back to the whole payload."""
        if not isinstance(response, dict):
            return str(response)

        params = response.get("params") or {}
        for value in (
            params.get("description"),
            params.get("responseMsg"),
            response.get("errorMessage"),
            response.get("message"),
            response.get("responseMsg"),
        ):
            if value:
                code = response.get("errorCode") or response.get("responseCode")
                return f"[{code}] {value}" if code else str(value)

        return frappe.as_json(response)

    def make_purchase(self, account_no, amount, invoice_id, payment_details=None, provider=None, **kwargs):
        """Make payment request to WaafiPay."""
        if self.staging == 1:
            return {
                "status": True,
                "transactionId": "1268666",
                "response": {
                    "responseCode": "2001",
                    "responseMsg": "RCS_SUCCESS",
                    "params": {
                        "state": "APPROVED",
                        "transactionId": "1268666"
                    }
                },
                "response_message": ""
            }

        data = self._build_request("API_PURCHASE", "WEB")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.get_password("merchant_key"),
            "paymentMethod": "MWALLET_ACCOUNT",
            "payerInfo": {"accountNo": account_no},
            "transactionInfo": {
                "invoiceId": str(invoice_id),
                "referenceId": str(invoice_id),
                "amount": f"{float(amount):.2f}",
                "currency": "USD",
                "description": "Easytouch POS"
            }
        }

        response = self._post(data)

        transactionId = None
        response_message = ""
        status = False

        try:
            if response.get("responseMsg") == "RCS_SUCCESS":
                status = True
                transactionId = response["params"]["transactionId"]
            else:
                response_message = self._extract_error_message(response)

        except Exception:
            frappe.log_error(
                title="WaafiPay Purchase Response Error",
                message=frappe.as_json(response)
            )
            response_message = f"Invalid response from WaafiPay: {frappe.as_json(response)}"

        frappe.logger("waafipay").info({
            "endpoint": self._get_endpoint(),
            "amount": amount,
            "invoice_id": invoice_id,
            "status": status,
            "transactionId": transactionId,
            "response_message": response_message,
            "response": response
        })

        safe_request = frappe.parse_json(frappe.as_json(data))
        safe_request["serviceParams"]["apiKey"] = "***"

        return {
            "status": status,
            "transactionId": transactionId,
            "response": response,
            "request": safe_request,
            "response_massage": response_message
        }

    def cancel_transaction(self, transaction_id, description="Cancel"):
        data = self._build_request("API_CANCELPURCHASE")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.get_password("merchant_key"),
            "transactionId": transaction_id,
            "description": description
        }
        response = self._post(data)
        self._handle_api_response("transactionId", data, response)
        return response

    def check_status(self, reference_id):
        data = self._build_request("API_TRANSACTIONSTATUS")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.get_password("merchant_key"),
            "referenceId": reference_id
        }
        response = self._post(data)

        # Log status check result
        frappe.logger("waafipay").info({
            "action": "check_status",
            "reference_id": reference_id,
            "response": response
        })

        return response

    def _handle_api_response(self, global_id, request_dict, response):
        if response.get(global_id):
            req_name = response[global_id]
            error = None
        else:
            req_name = response.get("requestId") or frappe.generate_hash(length=10)
            error = response

        if not frappe.db.exists("Integration Request", req_name):
            from frappe.integrations.utils import create_request_log
            create_request_log(request_dict, "Host", "WaafiPay", req_name, error)

        if error:
            frappe.log_error(
                title="WaafiPay Transaction Error",
                message=frappe.as_json(error)
            )
            frappe.throw(_(response.get("message", "Transaction Error")), title=_("Transaction Error"))


def create_mode_of_payment(gateway, payment_type="General"):
    with erpnext_app_import_guard():
        from erpnext import get_default_company

    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": gateway}, ["payment_account"]
    )

    mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
    if not mode_of_payment and payment_gateway_account:
        mode_of_payment = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": gateway,
            "enabled": 1,
            "type": payment_type,
            "accounts": [
                {
                    "doctype": "Mode of Payment Account",
                    "company": get_default_company(),
                    "default_account": payment_gateway_account,
                }
            ]
        })
        mode_of_payment.insert(ignore_permissions=True)
        return mode_of_payment
    elif mode_of_payment:
        return frappe.get_doc("Mode of Payment", mode_of_payment)


@frappe.whitelist()
def pay_with_waafi(account_no, amount, invoice_id):
    settings = frappe.get_single("WaafiPay")
    try:
        result = settings.make_purchase(account_no, amount, invoice_id)
        frappe.logger("waafipay").info({
            "function": "pay_with_waafi",
            "account_no": account_no,
            "amount": amount,
            "invoice_id": invoice_id,
            "result": result
        })
        return result
    except Exception:
        frappe.log_error(
            title="WaafiPay Payment Failure",
            message=frappe.get_traceback()
        )
        return {"status": False, "message": "Internal Server Error"}
