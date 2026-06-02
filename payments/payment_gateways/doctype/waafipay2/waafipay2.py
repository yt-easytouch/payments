import frappe
import json
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt
from frappe.integrations.utils import create_request_log
from payments.utils import erpnext_app_import_guard, create_payment_gateway
from .waafipay2_connector import WaafiPay2Connector

class WaafiPay2(Document):
    
    supported_currencies = ("USD",)

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. WaafiPay2 does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def on_update(self):
        # Create Payment Gateway record automatically
        create_payment_gateway(
            "WaafiPay2-" + self.gateway_name,
            settings="WaafiPay2",
            controller=self.gateway_name,
        )
        call_hook_method(
            "payment_gateway_enabled", gateway="WaafiPay2-" + self.gateway_name, payment_channel="Phone"
        )
        
        # Ensure we have a Mode of Payment
        self.create_mode_of_payment("WaafiPay2-" + self.gateway_name)

    def create_mode_of_payment(self, gateway, payment_type="General"):
        if not frappe.db.exists("Mode of Payment", gateway):
            with erpnext_app_import_guard():
                from erpnext import get_default_company

            payment_gateway_account = frappe.db.get_value(
                "Payment Gateway Account", {"payment_gateway": gateway}, ["payment_account"]
            )

            if payment_gateway_account:
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
        return None

    def connector(self):
        """
        Get WaafiPay2 connector instance
        """
        return WaafiPay2Connector(
            doc_data=self,
            merchant_id=self.merchant_id,
            user_id=self.user_id,
            merchant_key=self.get_password("merchant_key"),
            staging=self.staging,
            endpoint=self.endpoint
        )

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
        
        metadata = kwargs.get("metadata") or {}
        metadata.update({
            "reference": reference,
            "reference_doctype": reference_doctype,
            "reference_docname": reference_docname,
        })

        self.validate_transaction_currency(currency)

        provider = kwargs.get("payment_method")
        if not provider and reference_doctype == "Payment Request" and reference_docname:
            try:
                pr = frappe.get_doc("Payment Request", reference_docname)
                if pr.message:
                    provider = json.loads(pr.message or "{}").get("provider")
            except Exception:
                provider = None

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

    def make_purchase(self, account_no, amount, invoice_id, provider=None, **kwargs):   
        """Make payment request to WaafiPay2."""
        if not self.enabled:
            frappe.throw(_("WaafiPay2 is disabled."))

        req_id = frappe.generate_hash(length=10)
        
        # Prepare metadata
        metadata = kwargs.get("metadata") or {}
        if self.staging:
            metadata["test_mode"] = True
        
        # Log Request
        request_dict = {
            "account_no": account_no,
            "amount": flt(amount),
            "invoice_id": invoice_id,
            "provider": provider,
            "metadata": metadata,
            "request_amount": flt(amount)
        }
        
        create_request_log(
            request_dict,
            "Host",
            "WaafiPay2",
            req_id,
            None,
            reference_doctype=kwargs.get("reference_doctype"),
            reference_docname=kwargs.get("reference_docname"),
        )

        try:
            connector = self.connector()
            response = connector.make_purchase(
                account_no=account_no,
                amount=amount,
                invoice_id=invoice_id,
                description=f"Easytouch POS - {invoice_id}"
            )
            
            self.handle_api_response(req_id, request_dict, response)
            
            status = False
            transaction_id = None
            response_message = ""

            if response.get("responseMsg") == "RCS_SUCCESS":
                status = True
                transaction_id = response.get("params", {}).get("transactionId")
            else:
                # Handle "Already successfully completed" case (Duplicate Reference ID)
                response_code = response.get("responseCode")
                error_code = response.get("errorCode")
                response_msg_text = response.get("responseMsg")
                
                if (response_code == "5313" and error_code == "E10415") or response_msg_text == "This order has already been completed successfully":
                    params = response.get("params") or {}
                    if params.get("referenceId") == invoice_id and flt(params.get("txAmount")) == flt(amount):
                        status = True
                        transaction_id = params.get("transactionId")

                if not status:
                    response_message = response.get("params", {}).get("description") or response.get("message") or "Unknown Error"

            return {
                "status": status,
                "transactionId": transaction_id,
                "response": response,
                "response_massage": response_message
            }

        except Exception as e:
            error_message = str(e)
            frappe.log_error(title="WaafiPay2 Purchase Error", message=frappe.get_traceback())
            
            # Update log on failure
            if frappe.db.exists("Integration Request", req_id):
                ir = frappe.get_doc("Integration Request", req_id)
                ir.status = "Failed"
                ir.error = error_message
                ir.save(ignore_permissions=True)
            
            return {"status": False, "message": error_message}

    def handle_api_response(self, req_id, request_dict, response):
        """Update Integration Request and Payment Request based on API response."""
        if not frappe.db.exists("Integration Request", req_id):
            return

        ir = frappe.get_doc("Integration Request", req_id)
        
        response_msg = response.get("responseMsg")
        response_code = response.get("responseCode")
        error_code = response.get("errorCode")

        # Check for success OR the specific "already successfully completed" case
        is_success = response_msg == "RCS_SUCCESS"

        if not is_success and ((response_code == "5313" and error_code == "E10415") or response_msg == "This order has already been completed successfully"):
            # Possible duplicate/already success case. Verify amount and reference.
            params = response.get("params") or {}
            ret_ref = params.get("referenceId")
            ret_amount = flt(params.get("txAmount"))
            
            orig_ref = request_dict.get("invoice_id")
            orig_amount = flt(request_dict.get("amount"))
            
            if ret_ref == orig_ref and ret_amount == orig_amount:
                is_success = True
        
        if is_success:
            ir.status = "Completed"
        else:
            ir.status = "Failed"
            ir.error = response.get("params", {}).get("description") or response.get("message") or "Payment failed"

        ir.output = json.dumps(response)
        ir.save(ignore_permissions=True)

        # Update Payment Request status if failed
        if ir.status == "Failed":
            reference_doctype = request_dict.get("reference_doctype") or ir.reference_doctype
            reference_docname = request_dict.get("reference_docname") or ir.reference_docname
            
            if reference_doctype == "Payment Request" and reference_docname:
                if frappe.db.exists("Payment Request", reference_docname):
                    try:
                        pr = frappe.get_doc("Payment Request", reference_docname)
                        pr.db_set("status", "Failed")
                    except Exception as pr_error:
                        frappe.log_error(f"Error updating Payment Request status: {str(pr_error)}")

    def cancel_transaction(self, transaction_id, description="Cancel"):
        connector = self.connector()
        return connector.cancel_purchase(transaction_id, description)

    def check_status(self, reference_id):
        connector = self.connector()
        return connector.check_status(reference_id)
