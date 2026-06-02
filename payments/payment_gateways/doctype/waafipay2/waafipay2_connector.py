import requests
import uuid
import time
import frappe
from frappe import _

class WaafiPay2Connector:
    """
    Connector class for WaafiPay API (Version 2)
    Handles authentication and API calls.
    """

    def __init__(self, doc_data=None, merchant_id=None, user_id=None, merchant_key=None, staging=False, endpoint=None):
        self.doc_data = doc_data
        self.merchant_id = merchant_id
        self.user_id = user_id
        self.merchant_key = merchant_key
        self.staging = staging
        self.endpoint = endpoint or "https://api.waafipay.net/asm"
        
        if staging:
            self.endpoint = "http://sandbox.waafipay.net/asm"

        self.status = "Disconnect"
        self.authenticated = False

        if self.merchant_id and self.user_id and self.merchant_key:
            self.authenticated = True
            self.status = "Connect"
        
        # Update doc status if doc_data provided
        if self.doc_data and hasattr(self.doc_data, 'status'):
            if self.doc_data.status != self.status:
                self.doc_data.db_set("status", self.status)

    def _build_base_request(self, service_name, source="WEB"):
        return {
            "schemaVersion": "1.0",
            "requestId": str(uuid.uuid4()),
            "timestamp": str(int(time.time())),
            "channelName": source,
            "serviceName": service_name
        }

    def _get_headers(self):
        return {"Content-Type": "application/json"}

    def _post(self, payload):
        try:
            response = requests.post(self.endpoint, json=payload, headers=self._get_headers(), timeout=50)
            response.raise_for_status()
            res = response.json()

            # Log successful API call
            frappe.logger("waafipay2").info({
                "status": "OK",
                "endpoint": self.endpoint,
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
                title="WaafiPay2 Request Error",
                message=frappe.as_json(error_data)
            )
            return error_data
        except Exception as e:
             frappe.log_error(
                title="WaafiPay2 Unexpected Error",
                message=frappe.get_traceback()
            )
             return {"status": False, "message": str(e), "payload": payload}

    def make_purchase(self, account_no, amount, invoice_id, description="Easytouch POS", **kwargs):
        """
        Make API_PURCHASE request.
        """
        data = self._build_base_request("API_PURCHASE", "WEB")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.merchant_key,
            "paymentMethod": "MWALLET_ACCOUNT",
            "payerInfo": {"accountNo": account_no},
            "transactionInfo": {
                "invoiceId": str(invoice_id),
                "referenceId": str(invoice_id),
                "amount": f"{float(amount):.2f}",
                "currency": "USD",
                "description": description
            }
        }

        return self._post(data)

    def cancel_purchase(self, transaction_id, description="Cancel"):
        """
        Make API_CANCELPURCHASE request.
        """
        data = self._build_base_request("API_CANCELPURCHASE")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.merchant_key,
            "transactionId": transaction_id,
            "description": description
        }
        return self._post(data)

    def check_status(self, reference_id):
        """
        Make API_TRANSACTIONSTATUS request.
        """
        data = self._build_base_request("API_TRANSACTIONSTATUS")
        data["serviceParams"] = {
            "merchantUid": self.merchant_id,
            "apiUserId": self.user_id,
            "apiKey": self.merchant_key,
            "referenceId": reference_id
        }
        return self._post(data)
