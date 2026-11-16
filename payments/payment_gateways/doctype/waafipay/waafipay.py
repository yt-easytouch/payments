import frappe
import hashlib
import hmac
import json
import uuid
import time
import requests
from urllib.parse import quote
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
from werkzeug.wrappers import Response
from payments.utils import erpnext_app_import_guard

HPP_RETURN_METHOD = "payments.payment_gateways.doctype.waafipay.waafipay.hpp_return"
HPP_REJECTED_STATES = ("declined", "failed", "cancelled", "canceled", "rejected", "expired")
HPP_WEBHOOK_METHOD = "payments.payment_gateways.doctype.waafipay.waafipay.webhook"
WEBHOOK_TOLERANCE_SECONDS = 300


def mask_secrets(payload):
    safe = frappe.parse_json(frappe.as_json(payload or {}))
    params = safe.get("serviceParams") or {}
    for key in ("apiKey", "hppKey"):
        if params.get(key):
            params[key] = "***"
    return safe


def public_url(path):
    """WaafiPay and the phone must reach these; site_config `waafipay_public_url` (e.g. a tunnel) wins over the site URL."""
    base = frappe.conf.get("waafipay_public_url")
    return f"{base.rstrip('/')}{path}" if base else get_url(path)

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
            # (connect, read): without a timeout a stalled WaafiPay pins the web worker forever
            response = requests.post(endpoint, json=payload, headers=headers, timeout=(5, 30))
            response.raise_for_status()
            res = response.json()

            frappe.logger("waafipay").info({
                "status": "OK",
                "endpoint": endpoint,
                "payload": mask_secrets(payload),
                "response": res
            })

            return res

        except requests.RequestException as e:
            frappe.log_error(
                title="WaafiPay Request Error",
                message=frappe.as_json({"error": str(e), "payload": mask_secrets(payload)})
            )
            if isinstance(e, (requests.Timeout, requests.ConnectionError)):
                message = _("WaafiPay is not responding right now. Please try again in a moment.")
            else:
                message = _("WaafiPay could not take the payment right now. Please try again.")
            return {"status": False, "message": message, "payload": mask_secrets(payload)}

        except Exception as e:
            frappe.log_error(
                title="WaafiPay Unexpected Error",
                message=frappe.get_traceback()
            )
            return {"status": False, "message": str(e), "payload": mask_secrets(payload)}

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
        if self.use_hpp:
            return self.make_hpp_purchase(account_no, amount, invoice_id)

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

    def _hpp_params(self, **params):
        return {
            "merchantUid": self.merchant_id,
            "storeId": self.store_id,
            "hppKey": self.get_password("hpp_key"),
            **params,
        }

    def make_hpp_purchase(self, account_no, amount, invoice_id):
        """Open a Hosted Payment Page; the payer pays there and check_payment confirms it."""
        return_url = public_url(f"/api/method/{HPP_RETURN_METHOD}")
        data = self._build_request("HPP_PURCHASE")
        data["serviceParams"] = self._hpp_params(
            paymentMethod="MWALLET_ACCOUNT",
            hppSuccessCallbackUrl=f"{return_url}?result=success",
            hppFailureCallbackUrl=f"{return_url}?result=failure",
            hppRespDataFormat=2,
            transactionInfo={
                # A retried order must not reuse a reference WaafiPay has already seen.
                "referenceId": f"{invoice_id}-{frappe.generate_hash(length=6)}",
                "amount": f"{float(amount):.2f}",
                "currency": "USD",
                "description": f"Payment for {invoice_id}",
            },
        )
        if account_no:
            number = str(account_no).lstrip("0")
            data["serviceParams"]["payerInfo"] = {
                "subscriptionId": number if number.startswith("252") else f"252{number}"
            }
        response = self._post(data)
        params = response.get("params") or {}

        if response.get("responseMsg") != "RCS_SUCCESS" or not params.get("hppUrl"):
            return {
                "status": False,
                "response": response,
                "response_massage": self._extract_error_message(response),
            }

        return {
            "status": False,
            "pending": True,
            "invoiceId": params.get("referenceId") or data["serviceParams"]["transactionInfo"]["referenceId"],
            "payment_url": params["hppUrl"],
            "response": response,
            "response_massage": _("Complete the payment on the WaafiPay page."),
        }

    def get_hpp_transaction(self, reference_id):
        data = self._build_request("HPP_GETTRANINFO")
        data["serviceParams"] = self._hpp_params(referenceId=reference_id)
        return self._post(data)

    def check_payment(self, transaction_id):
        """Check a Hosted Payment Page once; callers repeat it while `pending` is true."""
        response = self.get_hpp_transaction(transaction_id)
        params = response.get("params") or {}
        state = str(params.get("status") or params.get("tranStatusDesc") or "").lower()
        is_paid = response.get("responseMsg") == "RCS_SUCCESS" and state == "approved"
        is_rejected = state in HPP_REJECTED_STATES

        return {
            "status": is_paid,
            "pending": not (is_paid or is_rejected),
            "rejected": is_rejected,
            "transactionId": params.get("transactionId"),
            "response": response,
            "response_massage": "" if is_paid else self._extract_error_message(response),
        }

    def reverse_payment(self, transaction_id):
        """Refund a Hosted Payment Page charge in full."""
        params = self.get_hpp_transaction(transaction_id).get("params") or {}
        data = self._build_request("HPP_REFUNDPURCHASE")
        data["serviceParams"] = self._hpp_params(
            referenceId=transaction_id,
            amount=str(params.get("amount") or params.get("tranAmount") or ""),
            description="Order refund",
        )
        response = self._post(data)
        state = str((response.get("params") or {}).get("state") or "").lower()
        return {
            "status": response.get("responseMsg") == "RCS_SUCCESS" and state == "approved",
            "response": response,
            "response_massage": self._extract_error_message(response),
        }

    @property
    def webhook_url(self):
        if self.is_new():
            return ""
        return public_url(f"/api/method/{HPP_WEBHOOK_METHOD}?gateway={quote(self.name)}")

    @frappe.whitelist()
    def register_webhook(self):
        """Register this gateway's webhook URL with WaafiPay, or point the existing one here."""
        frappe.only_for("System Manager")
        if not self.webhook_url.startswith("https://"):
            frappe.throw(_("WaafiPay needs a public HTTPS address. This site's URL is {0}.").format(self.webhook_url))

        service = "WEBHOOK_UPDATE" if self.webhook_id else "WEBHOOK_REGISTER"
        data = self._build_request(service)
        data["serviceParams"] = self._hpp_params(url=self.webhook_url, description=f"Easytouch {self.name}")
        if self.webhook_id:
            data["serviceParams"]["webhookId"] = self.webhook_id

        response = self._post(data)
        params = response.get("params") or {}
        if response.get("responseMsg") != "RCS_SUCCESS":
            frappe.throw(self._extract_error_message(response), title=_("Webhook not registered"))

        # WaafiPay returns the secret only when it creates one.
        if params.get("secret"):
            self.webhook_secret = params["secret"]
        self.webhook_id = str(params.get("webhookId") or self.webhook_id or "")
        self.save()
        return {"webhook_id": self.webhook_id, "webhook_url": self.webhook_url}

    def verify_webhook(self, timestamp, event_id, signature, body: bytes) -> bool:
        secret = self.get_password("webhook_secret", raise_exception=False)
        if not (secret and timestamp and event_id and signature):
            return False
        try:
            if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
                return False
        except ValueError:
            return False
        expected = hmac.new(secret.encode(), f"{timestamp}.{event_id}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip().lower())

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
    # ERPNext Mode of Payment glue is skipped when ERPNext is not installed.
    if "erpnext" not in frappe.get_installed_apps():
        return None

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


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def hpp_return(result=None, **kwargs):
    """Where WaafiPay sends the payer back. It tells the app; the payment is only trusted after check_payment."""
    result = "success" if result == "success" else "failure"
    message = _("Payment received. Finishing your order…") if result == "success" else _("The payment was not completed.")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:16px -apple-system,system-ui,sans-serif;color:#333;text-align:center;padding:24px;box-sizing:border-box}}</style>
</head><body><p>{frappe.utils.escape_html(message)}</p>
<script>var m={{type:"waafipay-hpp",result:"{result}"}};
try{{if(window.opener){{window.opener.postMessage(m,"*");setTimeout(function(){{window.close()}},800)}}else if(window.parent!==window){{window.parent.postMessage(m,"*")}}}}catch(e){{}}</script>
</body></html>"""
    return Response(html, content_type="text/html; charset=utf-8")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(**kwargs):
    """WaafiPay payment events. Signed with the webhook secret; apps confirm through check_payment."""
    headers = frappe.request.headers
    body = frappe.request.get_data()
    # A JSON body replaces form_dict, so the query string has to be read directly.
    gateway = frappe.request.args.get("gateway")
    if not (gateway and frappe.db.exists("WaafiPay", gateway)):
        return Response("unknown gateway", status=404)

    settings = frappe.get_doc("WaafiPay", gateway)
    if not settings.verify_webhook(
        headers.get("X-Webhook-Timestamp"),
        headers.get("X-Webhook-Event-Id"),
        headers.get("X-Webhook-Signature"),
        body,
    ):
        frappe.logger("waafipay").warning({"webhook": "bad signature", "gateway": gateway})
        return Response("invalid signature", status=401)

    event = json.loads(body or b"{}")
    payment = event.get("payment") or {}
    frappe.logger("waafipay").info({"webhook": event, "event_id": headers.get("X-Webhook-Event-Id")})

    # Answer 200 even when handling fails; the polling and the log pick it up instead of 8 retries.
    if payment.get("reference_id"):
        frappe.set_user("Administrator")
        for method in frappe.get_hooks("payment_gateway_webhook"):
            try:
                frappe.get_attr(method)(reference_id=payment["reference_id"], event=event)
            except Exception:
                frappe.db.rollback()
                frappe.log_error(title=f"WaafiPay webhook handler failed: {method}")

    return Response("ok", status=200)


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
