# etpos/etpos/waafipay/waafi_pay.py

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
        try:
            headers = {"Content-Type": "application/json"}
            response = requests.post(self._get_endpoint(), json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            return {
                "status": False,
                "message": str(e),
                "payload": payload
            }

    def make_purchase(self, account_no, amount, invoice_id, source="WEB"):
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
        response_massage=""
        status=False
        if response.get("responseMsg")=="RCS_SUCCESS":
            status = True
            transactionId = response['params']["transactionId"]
        else:
            response_massage = response['params']['description']
        
        return {"status":status, "transactionId":transactionId,"response":response ,"response_massage":response_massage}

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
        return self._post(data)

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
            frappe.throw(_(response.get("message", "Transaction Error")), title=_("Transaction Error"))

    # def on_update(self):
    #     from payments.utils import create_payment_gateway
    #     create_payment_gateway(
    #         "WaafiPay",
    #         settings="WaafiPay",
    #         controller="WaafiPay"
    #     )
    #     call_hook_method("payment_gateway_enabled", gateway="WaafiPay", payment_channel="Phone")
    #     frappe.db.commit()
    #     create_mode_of_payment("WaafiPay", payment_type="Phone")


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
    return settings.make_purchase(account_no, amount, invoice_id)
