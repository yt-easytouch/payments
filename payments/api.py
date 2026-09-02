import time
import frappe
import json
from contextlib import contextmanager

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry


@contextmanager
def ignore_permissions():
	"""Run a block with ERPNext's role checks disabled.

	Website/POS users carry no Frappe roles, so ERPNext helpers that resolve
	accounts on their behalf (e.g. get_payment_entry -> "User don't have
	permissions to select/read this account.") reject them. Keep the block as
	tight as possible - it disables permission checks process-wide while open.

	The flag alone is not enough: several helpers call frappe.has_permission
	with throw=True directly, and core never consults the flag there.
	DatabaseQuery needs its own arg too - frappe.get_list("Account", ...) reads
	self.flags.ignore_permissions, never the global one.
	"""
	previous = frappe.flags.ignore_permissions
	original_has_permission = frappe.has_permission
	original_execute = frappe.model.db_query.DatabaseQuery.execute

	def execute_ignoring_permissions(self, *args, **kwargs):
		if len(args) < 12:
			kwargs["ignore_permissions"] = True
		return original_execute(self, *args, **kwargs)

	frappe.flags.ignore_permissions = True
	frappe.has_permission = lambda *args, **kwargs: True
	frappe.model.db_query.DatabaseQuery.execute = execute_ignoring_permissions
	try:
		yield
	finally:
		frappe.model.db_query.DatabaseQuery.execute = original_execute
		frappe.has_permission = original_has_permission
		frappe.flags.ignore_permissions = previous


# ─────────────────────────────────────────────────────────────────────────────
#  Failure classification keywords
#  Expand these lists to match your gateway's exact error vocabulary.
# ─────────────────────────────────────────────────────────────────────────────

CUSTOMER_FAULT_KEYWORDS = [
    "insufficient", "not enough", "balance", "low balance",
    "wrong pin", "invalid pin", "incorrect pin", "pin mismatch",
    "declined", "rejected", "blocked", "limit exceeded",
    "account not found", "invalid account", "unauthorized",
    "invalid number", "number not registered", "user not found",
]

SYSTEM_FAULT_KEYWORDS = [
    "timeout", "timed out", "connection", "network", "unreachable",
    "502", "503", "504", "500", "internal server error", "gateway error",
    "service unavailable", "try again", "temporary", "upstream",
    "no response", "request failed", "socket", "read error",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def remove_country_code(phone: str) -> str:
    """Strip +252 or 252 prefix from a Somali phone number."""
    phone = str(phone).strip()
    for prefix in ("+252", "252"):
        if phone.startswith(prefix):
            return phone[len(prefix):]
    return phone


def _load_doc(name: str):
    """Return a Sales Invoice or Sales Order doc, or throw a clear error."""
    for dt in ("Sales Invoice", "Sales Order"):
        if frappe.db.exists(dt, name):
            return frappe.get_doc(dt, name)
    frappe.throw(f"Document '{name}' not found as Sales Invoice or Sales Order.")


def _get_company_account(company: str, account_name: str):
    """Look up an account by name within a company."""
    return frappe.get_value(
        "Account", {"account_name": account_name, "company": company}, "name"
    )


def classify_failure(gateway_res: dict) -> str:
    """
    Classify a failed gateway response as 'customer' or 'system'.

    - 'customer' → wrong PIN, insufficient balance, declined, etc.
      Action: cancel / delete the Sales Invoice immediately.

    - 'system'   → timeout, network error, 5xx, etc.
      Action: auto-retry; only cancel after all retries are exhausted.

    Defaults to 'customer' when ambiguous — safer than retrying unknown errors.
    """
    message = (
        gateway_res.get("response_massage") or
        gateway_res.get("message") or
        gateway_res.get("error") or ""
    ).lower()

    for kw in SYSTEM_FAULT_KEYWORDS:
        if kw in message:
            return "system"

    return "customer"


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────

def _build_log_payload(payload: dict = None, gateway_response: dict = None) -> str:
    """Serialize the request payload together with the raw gateway response."""
    data = {"request": payload or {}}
    if gateway_response is not None:
        data["gateway_response"] = gateway_response
    try:
        return json.dumps(data, indent=2, default=str)
    except Exception:
        return json.dumps({"request": str(payload), "gateway_response": str(gateway_response)})


def _log_failure(
    doc_name: str,
    reason: str,
    payload: dict = None,
    fault_type: str = "customer",
    gateway_response: dict = None,
):
    """
    Write a structured entry to the Payment Logs doctype.
    Never raises — logging must not crash the payment flow.
    """
    try:
        frappe.get_doc({
            "doctype":            "Payment Logs",
            "reference_document": doc_name,
            "reason":             reason,
            "fault_type":         fault_type,
            "status":             "Failed",
            "payload":            _build_log_payload(payload, gateway_response),
            "timestamp":          frappe.utils.now(),
            "user":               frappe.session.user,
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "PaymentFailureLog insert failed")


def _log_success(
    doc_name: str,
    payload: dict = None,
    reason: str = "Payment successful",
    gateway_response: dict = None,
):
    """
    Write a success entry to the Payment Logs doctype.
    Never raises — logging must not crash the payment flow.
    """
    try:
        doc = frappe.get_doc({
            "doctype":            "Payment Logs",
            "reference_document": doc_name,
            "reason":             reason,
            "fault_type":         "system",
            "status":             "Active",
            "payload":            _build_log_payload(payload, gateway_response),
            "timestamp":          frappe.utils.now(),
            "user":               frappe.session.user,
        })
        doc.insert(ignore_permissions=True)
        frappe.db.set_value("Payment Logs", doc.name, "docstatus", 1)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "PaymentSuccessLog insert failed")


# ─────────────────────────────────────────────────────────────────────────────
#  Invoice lifecycle on failure
# ─────────────────────────────────────────────────────────────────────────────

def _cancel_or_delete_invoice(invoice_name: str, reason: str):
    """
    Customer fault path:
      - Draft invoice  (docstatus=0) → delete entirely.
      - Submitted invoice (docstatus=1) that is fully unpaid → cancel.
      - Partially paid invoice → leave it alone (money was already taken).
    """
    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)

        # Never touch a partially-paid invoice
        if si.outstanding_amount < si.grand_total:
            frappe.logger().info(
                f"[Payment] Skipping cancel/delete for {invoice_name} "
                f"— partially paid (outstanding={si.outstanding_amount})"
            )
            return

        si.flags.ignore_permissions = True

        if si.docstatus == 0:
            # Still a draft — just delete it
            si.delete()
            frappe.db.commit()
           
        elif si.docstatus == 1:
            # Submitted but unpaid — cancel it
            si.cancel()
            frappe.db.commit()
           

    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to cancel/delete invoice {invoice_name}")


def _mark_invoice_retry_pending(invoice_name: str, reason: str):
    """
    System fault path: all retries exhausted.
    Leave the invoice intact but flag it so staff know it needs attention.
    """
    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)
        if si.meta.has_field("custom_payment_status"):
            if si.docstatus == 0:
                si.custom_payment_status = "Retry Pending"
                si.save(ignore_permissions=True)
            else:
                si.db_set("custom_payment_status", "Retry Pending")
            frappe.db.commit()
        frappe.logger().warning(
            f"[Payment] System fault for {invoice_name} after all retries: {reason}"
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to mark retry pending for {invoice_name}")


def _create_hold_order_from_invoice(doc, mode_of_payment=None) -> str | None:
    """
    Snapshot a draft Sales Invoice into an etpos Hold Order so the cart can be
    resumed from the POS UI later. Mirrors etpos' create_hold_order mapping:
    the full invoice dict goes into hold.data, metadata lands on columns the
    Hold Orders list filters on. Returns the Hold Order name, or None when
    etpos is not installed.
    """
    if not frappe.db.exists("DocType", "Hold Order"):
        return None

    inv_data = doc.as_dict()
    inv_data.update({
        "source":               doc.get("custom_source"),
        "salesperson":          doc.get("ytpos_salesperson") or frappe.session.user,
        "pos_opening_shift":    doc.get("posa_pos_opening_shift"),
        "table":                doc.get("custom_table"),
        "order_type":           doc.get("custom_order_type"),
        "notification_type":    doc.get("custom_notification_type"),
        "notification_status":  doc.get("custom_notification_status"),
        "total_items_amount":   doc.get("total"),
        "invoice_status":       "Draft",
        "is_from_ui":           0,
    })

    with ignore_permissions():
        hold = frappe.new_doc("Hold Order")
        hold.data                 = frappe.as_json(inv_data)
        hold.pos_profile          = doc.get("pos_profile")
        hold.user                 = inv_data["salesperson"]
        hold.queue_number         = doc.get("custom_order_number")
        hold.source               = inv_data["source"]
        hold.pos_shift            = inv_data["pos_opening_shift"]
        hold.table                = inv_data["table"]
        hold.invoice_status       = inv_data["invoice_status"]
        hold.is_from_ui           = 0
        hold.order_type           = inv_data["order_type"]
        hold.notification_type    = inv_data["notification_type"]
        hold.notification_status  = inv_data["notification_status"]
        hold.total_items_amount   = inv_data["total_items_amount"]
        hold.note                 = doc.get("referenceNote")
        hold.currency             = doc.get("currency")
        # Keep the customer's chosen payment method visible to the cashier even
        # when nothing was charged yet (0-amount rows are dropped from SI).
        hold.mode_of_payment      = (
            mode_of_payment
            or next(
                (
                    p.get("mode_of_payment")
                    for p in (doc.get("payments") or [])
                    if p.get("mode_of_payment")
                ),
                None,
            )
        )
        hold.insert(ignore_permissions=True)
    return hold.name


def _repoint_references(old_name: str, new_doctype: str, new_name: str):
    """Move Payment Logs / Payment Request references onto another document."""
    frappe.db.sql(
        """UPDATE `tabPayment Logs`
           SET reference_document = %s
           WHERE reference_document = %s""",
        (new_name, old_name),
    )
    frappe.db.sql(
        """UPDATE `tabPayment Request`
           SET reference_doctype = %s, reference_name = %s
           WHERE reference_doctype = 'Sales Invoice' AND reference_name = %s""",
        (new_doctype, new_name, old_name),
    )


def _hold_order_and_delete_invoice(doc, reason: str, mode_of_payment=None) -> str | None:
    """
    Non-POS checkout path (APP / WEB / KIOSK paying cash or another plain
    mode): park the cart as a Hold Order for cashier follow-up and remove
    the auto-created draft Sales Invoice. When staff later complete the held
    order, etpos repoints these references back onto the new invoice and
    deletes the hold. Never raises — payment flow must continue cleanly.
    """
    try:
        hold_name = _create_hold_order_from_invoice(doc, mode_of_payment=mode_of_payment)
        if not hold_name:
            return None

        _repoint_references(doc.name, "Hold Order", hold_name)

        si = frappe.get_doc("Sales Invoice", doc.name)
        si.flags.ignore_permissions = True
        if si.docstatus == 1:
            si.cancel()
        si.delete()
        frappe.db.commit()

        return hold_name

    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to hold/delete invoice {doc.name}",
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Core payment helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_payment_entry(
    doc,
    amount: float,
    mode_of_payment: str,
    reference_no: str,
    paid_to: str = None,
    paid_from: str = None,
    remarks: str = None,
):
    """Create and submit a Payment Entry linked to doc (SI or SO)."""
    if isinstance(doc, str):
        doc = _load_doc(doc)

    if not doc.get("company"):
        doc.company = (
            frappe.db.get_default("company")
            or frappe.get_all("Company", limit=1)[0].name
        )

    # get_payment_entry resolves party/bank accounts and calls frappe.has_permission
    # with throw=True; doc-level flags don't reach it, so wrap the whole block.
    with ignore_permissions():
        pe = get_payment_entry(
            doc.doctype, doc.name,
            party_amount=amount,
            bank_account=paid_to,
            bank_amount=amount,
        )

        pe.mode_of_payment = mode_of_payment
        pe.reference_no    = reference_no
        pe.reference_date  = frappe.utils.nowdate()
        pe.posting_date    = frappe.utils.nowdate()

        if remarks:   pe.remarks    = remarks
        if paid_from: pe.paid_from  = paid_from
        if paid_to:   pe.paid_to    = paid_to

        pe.set_missing_values()
        pe.flags.ignore_permissions = True
        pe.insert(ignore_permissions=True)
        pe.submit()
    frappe.db.commit()
    return pe


def create_payment_request(doc, amount: float, mode_of_payment: str, phone_number: str) -> dict:
    """
    Create a Payment Request (USSD / manual flow — no direct gateway).
    Returns a summary dict for the caller.
    """
    pr = frappe.get_doc({
        "doctype":                "Payment Request",
        "payment_request_type":   "Inward",
        "subject":                f"Payment Request for {doc.name}",
        "message":                f"Please pay for {doc.name}.",
        "transaction_date":       frappe.utils.nowdate(),
        "print_format":           "Standard",
        "party_type":             "Customer",
        "mode_of_payment":        mode_of_payment,
        "phone_number":           phone_number,
        "party":                  doc.customer,
        "party_name":             doc.customer_name,
        "reference_doctype":      doc.doctype,
        "reference_name":         doc.name,
        "grand_total":            amount,
        "currency":               doc.currency,
        "outstanding_amount":     amount,
        "party_account_currency": doc.currency,
        "cost_center":            doc.get("cost_center"),
        "status":                 "Draft",
    })
    with ignore_permissions():
        pr.insert(ignore_permissions=True)
        frappe.db.commit()
        pr.flags.ignore_permissions = True
        pr.submit()

    return {
        "success":      True,
        "request_name": pr.name,
        "customer":     doc.customer_name,
        "amount":       amount,
        "currency":     doc.currency,
        "status":       pr.status,
        "payment_url":  getattr(pr, "payment_url", None),
    }


def call_payment_gateway(
    mode_of_payment: str,
    amount: float,
    doc_name: str,
    phone_number: str,
) -> dict:
    """Single raw gateway call. Returns the gateway's response dict."""
    gateway_account = frappe.get_value(
        "Mode of Payment", mode_of_payment, "payment_gateway_account"
    )
    if not gateway_account:
        return {"status": False, "message": "Payment gateway not configured"}

    payment_gateway = frappe.get_value(
        "Payment Gateway Account", gateway_account, "payment_gateway"
    )
    gw   = frappe.get_doc("Payment Gateway", payment_gateway)
    ctrl = frappe.get_doc(gw.gateway_settings, gw.gateway_controller)
    return ctrl.make_purchase(phone_number, amount, doc_name)


def call_payment_gateway_with_retry(
    mode_of_payment: str,
    amount: float,
    doc_name: str,
    phone_number: str,
    max_retries: int = 3,
    delay_seconds: float = 2.0,
) -> dict:
    """
    Call the gateway up to max_retries times, but only retry on system faults.

    Customer faults (wrong PIN, insufficient funds, etc.) return immediately —
    retrying would just annoy the customer and waste gateway credits.

    Adds a '_fault_type' key ('customer' | 'system' | None) to the response
    so the caller knows how to handle failure.
    """
    last_res = {"status": False, "message": "No attempts made", "_fault_type": "system"}

    for attempt in range(1, max_retries + 1):
        try:
            res = call_payment_gateway(mode_of_payment, amount, doc_name, phone_number)
        except Exception as exc:
            # Any Python exception (network, import error, etc.) = system fault
            res = {"status": False, "message": f"Exception on attempt {attempt}: {str(exc)}"}

        if res.get("status"):
            res["_fault_type"] = None          # success — no fault
            return res

        fault_type = classify_failure(res)
        res["_fault_type"] = fault_type
        last_res = res

        if fault_type == "customer":
            # No point retrying — the customer needs to act
            frappe.logger().info(
                f"[Payment] Customer fault on attempt {attempt} for {doc_name}: "
                f"{res.get('message')}"
            )
            return res

        # System fault — log and wait before next attempt
        frappe.logger().warning(
            f"[Payment] System fault on attempt {attempt}/{max_retries} "
            f"for {doc_name}: {res.get('message')}"
        )
        if attempt < max_retries:
            time.sleep(delay_seconds)

    # All retries exhausted on system fault
    frappe.logger().error(
        f"[Payment] All {max_retries} retries exhausted for {doc_name}. "
        f"Last error: {last_res.get('message')}"
    )
    return last_res


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def process_payment(doc, payload):
    # ── Load document ──────────────────────────────────────────────────────────
    if isinstance(doc, str):
        doc = _load_doc(doc)

    if isinstance(payload, str):
        payload = json.loads(payload)

    # ── Parse payload ──────────────────────────────────────────────────────────
    phone          = remove_country_code(payload.get("payment_number") or "")
    wallet_amount  = float(payload.get("wallet_amount", 0))
    loyalty_points = int(payload.get("loyalty_points", 0))
    payments       = payload.get("payments", [])

    result = {
        "status":            True,
        "gateway_responses": [],
        "payment_requests":  [],
    }

    # ── 1. Wallet payment ──────────────────────────────────────────────────────
    if wallet_amount > 0:
        company        = doc.company
        wallet_account = (
            _get_company_account(company, "Customer Wallet")
            or frappe.get_value("Company", company, "default_cash_account")
        )
        debtors = frappe.get_value("Company", company, "default_receivable_account")
        try:
            create_payment_entry(
                doc=doc,
                amount=wallet_amount,
                mode_of_payment="Wallet",
                reference_no=f"WALLET-{doc.name}",
                paid_to=wallet_account,
                paid_from=debtors,
                remarks=f"Wallet payment for {doc.name}",
            )
            _log_success(doc.name, payload, f"Wallet payment of {wallet_amount} successful")
        except Exception:
            # Wallet failure is non-fatal — log and continue
            frappe.log_error(frappe.get_traceback(), "Wallet Payment Error")

    # ── 2. Loyalty points ──────────────────────────────────────────────────────
    if loyalty_points > 0:
        if doc.docstatus == 0:
            doc.redeem_loyalty_points = 1
            doc.loyalty_points        = loyalty_points
            doc.save(ignore_permissions=True)
        else:
            doc.db_set("redeem_loyalty_points", 1)
            doc.db_set("loyalty_points", loyalty_points)

    # ── 3. Gateway / USSD payments ─────────────────────────────────────────────
    for p in payments:
        amount = float(p.get("amount", 0))
        mop    = p.get("mode_of_payment", "")

        if amount <= 0 or not mop:
            continue

        has_gateway = bool(frappe.get_value("Mode of Payment", mop, "payment_gateway_account"))
        is_online_payment = bool(frappe.get_value("Mode of Payment", mop, "is_online_payment"))
        # ── USSD / manual: no gateway configured ──────────────────────────────
        if not has_gateway and is_online_payment:
            if not phone:
                _log_failure(doc.name, "Missing phone for USSD payment", payload, "customer")
                return {
                    "status":     False,
                    "fault_type": "customer",
                    "message":    "Phone number is required for USSD payment.",
                }

            pr_res = create_payment_request(doc, amount, mop, phone)
            result["payment_requests"].append(pr_res)
            continue
        if not is_online_payment and not has_gateway:
            # ── Non-POS sources (APP / WEB / KIOSK): don't settle the sale.
            # Park it as a Hold Order for cashier follow-up and delete the
            # draft invoice — staff resumes and completes it from the POS.
            if doc.doctype == "Sales Invoice" and doc.get("custom_source") != "POS":
                hold_name = _hold_order_and_delete_invoice(
                    doc, f"Non-gateway payment via {mop} for non-POS source",
                    mode_of_payment=mop,
                )
                return {
                    "status":     True,
                    "held":       True,
                    "hold_order": hold_name,
                    "message":    (
                        f"Order parked as Hold Order {hold_name}; "
                        f"invoice {doc.name} deleted."
                        if hold_name
                        else "Hold Order unavailable; invoice left untouched."
                    ),
                }

            txn_ref = f"PAYMENT-{doc.name}"
            if doc.meta.has_field("payments") and getattr(doc, "is_pos", 0):
                doc.append("payments", {
                    "mode_of_payment":   mop,
                    "amount":            amount,
                    "payment_reference": txn_ref,
                    "processed_by":      frappe.session.user,
                })
                doc.save(ignore_permissions=True)
            else:
                with ignore_permissions():
                    doc.submit()
                create_payment_entry(
                    doc=doc,
                    amount=amount,
                    mode_of_payment=mop,
                    reference_no=txn_ref,
                    remarks=f"Payment for {doc.name} via {mop}",
                )

            if doc.meta.has_field("custom_payment_status"):
                if doc.docstatus == 0:
                    doc.custom_payment_status = "Paid"
                    doc.save(ignore_permissions=True)
                else:
                    doc.db_set("custom_payment_status", "Paid")

            _log_success(doc.name, payload, f"Manual payment of {amount} via {mop} successful")
            continue
        # ── Direct gateway charge ─────────────────────────────────────────────
        if not phone:
            _log_failure(doc.name, "Missing phone for gateway payment", payload, "customer")
            return {
                "status":     False,
                "fault_type": "customer",
                "message":    "Phone number is required for gateway payment.",
            }

        gw_res = call_payment_gateway_with_retry(
            mode_of_payment=mop,
            amount=amount,
            doc_name=doc.name,
            phone_number=phone,
            max_retries=3,
            delay_seconds=2.0,
        )

        # ── Gateway failed ────────────────────────────────────────────────────
        if not gw_res.get("status"):
            fault_type = gw_res.get("_fault_type", "customer")
            reason     = (
                gw_res.get("response_massage") or
                gw_res.get("message") or
                json.dumps(gw_res.get("response") or {}, default=str) or
                "Payment failed"
            )

            _log_failure(doc.name, reason, payload, fault_type, gateway_response=gw_res)

            if doc.doctype == "Sales Invoice":
                if fault_type == "customer":
                    # Wrong PIN / low balance etc. — remove the invoice
                    _cancel_or_delete_invoice(doc.name, reason)
                else:
                    # System fault, all retries done — keep invoice, flag it
                    _mark_invoice_retry_pending(doc.name, reason)

            return {
                "status":     False,
                "fault_type": fault_type,
                "message":    reason,
                "retried":    fault_type == "system",
            }

        # ── Gateway succeeded ─────────────────────────────────────────────────
        txn_ref = (
            gw_res.get("payment_id") or
            gw_res.get("transactionId") or
            doc.name
        )

        if doc.meta.has_field("payments") and getattr(doc, "is_pos", 0):
            doc.append("payments", {
                "mode_of_payment":   mop,
                "amount":            amount,
                "payment_reference": txn_ref,
                "processed_by":      frappe.session.user,
            })
            doc.save(ignore_permissions=True)
        else:
            create_payment_entry(
                doc=doc,
                amount=amount,
                mode_of_payment=mop,
                reference_no=txn_ref,
            )

        if doc.meta.has_field("custom_payment_status"):
            if doc.docstatus == 0:
                doc.custom_payment_status = "Paid"
                doc.save(ignore_permissions=True)
            else:
                doc.db_set("custom_payment_status", "Paid")

        _log_success(
            doc.name,
            payload,
            f"Gateway payment of {amount} via {mop} successful (ref: {txn_ref})",
            gateway_response=gw_res,
        )
        result["gateway_responses"].append(gw_res)

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Backward-compatible public wrappers
#  These keep your existing API calls working without changes.
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def request_payment(payments=None, sales_invoice=None, data=None):
    """
    Legacy wrapper — routes to process_payment.
    Kept for any existing callers; prefer process_payment directly.
    """
    payments = payments or []
    if isinstance(payments, dict):
        payments = [payments]

    payload = {
        "payment_number": (data or {}).get("payment_number") or (data or {}).get("mobile"),
        "wallet_amount":  0,
        "loyalty_points": 0,
        "payments":       payments,
    }
    return process_payment(sales_invoice, payload)


def process_order_payments(sales_order, payment_method: dict, payment_allocation: dict) -> dict:
    """
    Legacy wrapper — routes to process_payment.
    Kept for any existing callers; prefer process_payment directly.
    """
    select_payment = payment_method.get("select_payment")
    payments = []

    if select_payment and payment_allocation.get("amount_with_payment_method", 0) > 0:
        payments.append({
            "mode_of_payment": select_payment,
            "amount":          payment_allocation["amount_with_payment_method"],
        })

    payload = {
        "payment_number": payment_method.get("payment_number"),
        "wallet_amount":  payment_allocation.get("wallet_used", 0) if payment_method.get("balance") else 0,
        "loyalty_points": payment_allocation.get("point_amount", 0) if payment_method.get("point") else 0,
        "payments":       payments,
    }

    res = process_payment(sales_order, payload)
    return {
        "status":        res.get("status", False),
        "redirect_page": "completed" if res.get("status") else None,
        "fault_type":    res.get("fault_type"),
        "message":       res.get("message"),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SMS webhook (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def receive_sms():
    from payments.payments.doctype.sms_inbox.sms_inbox import receive_sms as _recv
    data = frappe.request.get_json()
    frappe.local.request.data = json.dumps(data)
    return _recv()

# ─────────────────────────────────────────────────────────────────────────────
#  Payment Request helpers (kept from the original payments.api)
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_payment_from_invoice(sales_invoice,phone_number,mode_of_payment):
    if not frappe.db.exists("Sales Invoice", sales_invoice):
        return {"error": f"Sales Invoice {sales_invoice} not found"}
    
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if si.outstanding_amount <= 0:
        return {"error": f"Invoice {sales_invoice} has no outstanding amount"}

    # Create Payment Request
    pr = frappe.get_doc({
        "doctype": "Payment Request",
        "payment_request_type": "Inward",
        "subject": f"Payment Request for {si.name}",
        "message": f"Please click the link below to pay for your booking {si.name}.",
        "transaction_date": frappe.utils.nowdate(),
        "print_format": "Standard",
        "party_type": "Customer",
        "mode_of_payment":mode_of_payment,
        "phone_number":phone_number,
        "party": si.customer,
        "party_name": si.customer_name,
        "reference_doctype": "Sales Invoice",
        "reference_name": si.name,
        "grand_total": si.outstanding_amount,
        "currency": si.currency,
        "outstanding_amount": si.outstanding_amount,
        "party_account_currency": si.currency,
        "cost_center":si.cost_center,
        "status": "Draft"
    })
    
    pr.insert(ignore_permissions=True)
    frappe.db.commit()
    pr.flags.ignore_permissions = True
    pr.submit()
    return {
        "success": True,
        "payment_request": pr.name,
        "customer": si.customer_name,
        "amount": si.outstanding_amount,
        "currency": si.currency,
        "status": pr.status,
        "payment_url": pr.payment_url if hasattr(pr, 'payment_url') else None
    }


def process_payment_gateway(mode_of_payment, amount, invoice_name, phone_number):
    """Process payment through gateway"""
    gateway_account = frappe.get_value(
        "Mode of Payment", {"name": mode_of_payment}, "payment_gateway_account"
    )
    
    if not gateway_account:
        return {"status": False, "message": "Payment gateway not configured"}
    
    payment_gateway = frappe.get_value(
        "Payment Gateway Account", {"name": gateway_account}, "payment_gateway"
    )
    
    gateway_doc = frappe.get_doc("Payment Gateway", payment_gateway)
    gateway_controller = frappe.get_doc(gateway_doc.gateway_settings, gateway_doc.gateway_controller)
    
    return gateway_controller.make_purchase(phone_number, amount, invoice_name)

