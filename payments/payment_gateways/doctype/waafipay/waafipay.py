import frappe
import uuid
import time
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method
from payments.utils import erpnext_app_import_guard

class WaafiPay(Document):
    def _get_endpoint(self):
        return "http://sandbox.waafipay.net/asm" if self.staging == 1 else self.endpoint

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
            headers = {"Content-Type": "application/json"}
            response = requests.post(self._get_endpoint(), json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            res = response.json()

            # Log successful API call
            frappe.logger("waafipay").info({
                "status": "OK",
                "endpoint": self._get_endpoint(),
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

    def make_purchase(self, account_no, amount, invoice_id, source="WEB"):
        """Make payment request to WaafiPay."""
        if self.staging == 1:
            return {
                "status": True,
                "transactionId": f"DEMO{str(uuid.uuid4())[:6].upper()}",
                "response": {"responseMsg": "RCS_SUCCESS", "demo": True},
                "response_massage": "Demo Transaction Successful"
            }

        data = self._build_request("API_PURCHASE", source)
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
                response_message = response.get("params", {}).get("description", "Unknown Error")

        except Exception:
            frappe.log_error(
                title="WaafiPay Purchase Response Error",
                message=frappe.as_json(response)
            )
            response_message = "Invalid response from WaafiPay"

        # Log the full transaction
        frappe.logger("waafipay").info({
            "endpoint": self._get_endpoint(),
            "amount": amount,
            "invoice_id": invoice_id,
            "status": status,
            "transactionId": transactionId,
            "response_message": response_message,
            "response": response
        })

        return {
            "status": status,
            "transactionId": transactionId,
            "response": response,
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
