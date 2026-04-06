import frappe
import json

@frappe.whitelist()
def create_payment_from_invoice(sales_invoice,phone_number,mode_of_payment):
    # try:
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


def remove_country_code(phone_number):
    if phone_number.startswith("+252"):
        return phone_number[4:]
    return phone_number

def create_payment_entry(doc, amount, mode_of_payment, reference_no, paid_to=None, paid_from=None, remarks=None):
    if isinstance(doc, str):
        # Try to find the document if only the name was passed
        try:
            doc = frappe.get_doc("Sales Invoice", doc)
        except frappe.DoesNotExistError:
            doc = frappe.get_doc("Sales Order", doc)

    if not doc.get("company"):
        doc.company = frappe.db.get_default("company") or frappe.get_all("Company", limit=1)[0].name
        
    # Use standard ERPNext utility to create the basic Payment Entry with correct links
    # This automatically handles party-to-receivable linking (avoiding "Customer is required" error)
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    
    pe = get_payment_entry(
        doc.doctype, 
        doc.name, 
        party_amount=amount, 
        bank_account=paid_to,
        bank_amount=amount
    )

    # Set custom and additional fields manually
    pe.mode_of_payment = mode_of_payment
    pe.reference_no = reference_no
    pe.reference_date = frappe.utils.nowdate()
    pe.posting_date = frappe.utils.nowdate()
    
    if remarks:
        pe.remarks = remarks
        
    if paid_from:
        pe.paid_from = paid_from
        
    if paid_to:
        pe.paid_to = paid_to

    # Ensure all accounting fields are populated by ERPNext core logic
    pe.set_missing_values()

    pe.flags.ignore_permissions = True
    pe.insert(ignore_permissions=True)
    pe.submit()
    frappe.db.commit()
    return pe
@frappe.whitelist()
def process_payment(doc, payload):
    if isinstance(doc, str):
        # Attempt to load as Sales Invoice first, then Sales Order
        if frappe.db.exists("Sales Invoice", doc):
            doc = frappe.get_doc("Sales Invoice", doc)
        elif frappe.db.exists("Sales Order", doc):
            doc = frappe.get_doc("Sales Order", doc)
        else:
            return {"status": False, "message": f"Document {doc} not found"}

    if isinstance(payload, str):
        payload = json.loads(payload)

    payment_number = payload.get("payment_number")
    if payment_number:
        payment_number = remove_country_code(payment_number)
        
    wallet_amount = payload.get("wallet_amount", 0)
    loyalty_points = payload.get("loyalty_points", 0)
    payments = payload.get("payments", [])
    
    result = {
        "status": True,
        "payment_request_details": None,
        "gateway_responses": [],
        "redirect_page": "completed"
    }
    
    # 1. Handle Wallet Payment
    if wallet_amount > 0:
        try:
            company = doc.company
            debtors_account = frappe.get_value("Company", company, "default_receivable_account")
            wallet_account = frappe.get_value("Account", {"account_name": "Customer Wallet", "company": company}, "name")
            
            if not wallet_account:
                wallet_account = frappe.get_value("Company", company, "default_cash_account")
                
            payment_entry = create_payment_entry(
                doc=doc,
                amount=wallet_amount,
                mode_of_payment="Wallet",
                reference_no=f"WALLET-{doc.name}",
                paid_to=wallet_account,
                paid_from=debtors_account,
                remarks=f"Wallet payment for {doc.name}"
            )
            
            if doc.docstatus == 0:
                doc.append("payments", {
                    "mode_of_payment": "Wallet",
                    "processed_by": frappe.session.user,
                    "amount": wallet_amount,
                    "reference_no": payment_entry.name
                })
                doc.save(ignore_permissions=True)
            else:
                # For submitted docs, we just ensure the payment is linked via Payment Entry
                # We don't try to append to 'payments' table unless we have to
                pass
            
        except Exception as e:
            frappe.log_error(f"Error creating wallet payment entry: {str(e)}", "Wallet Payment Error")
            if doc.docstatus == 0:
                doc.append("payments", {
                    "mode_of_payment": "Wallet",
                    "processed_by": frappe.session.user,
                    "amount": wallet_amount,
                    "reference_no": f"WALLET-{doc.name}"
                })
                doc.save(ignore_permissions=True)
            
    # 2. Handle Loyalty Points
    if loyalty_points > 0:
        if doc.docstatus == 0:
            doc.redeem_loyalty_points = 1 
            doc.loyalty_points = loyalty_points
            doc.save(ignore_permissions=True)
        else:
            doc.db_set("redeem_loyalty_points", 1)
            doc.db_set("loyalty_points", loyalty_points)

    # 3. Handle Gateway & USSD Payments
    for p in payments:
        if p.get("amount") > 0 and payment_number:
            mode_of_payment = p.get("mode_of_payment")
            gateway_account = frappe.get_value("Mode of Payment", mode_of_payment, "payment_gateway_account")
            
            if not gateway_account:
                # No gateway -> Standard Frappe Payment Request (USSD/Manual)
                payment_res = create_payment_from_invoice(doc.name, payment_number, mode_of_payment)                        
                result["payment_request_details"] = payment_res
            else:
                # Direct API Gateway Account
                gateway_res = process_payment_gateway(mode_of_payment, p.get("amount"), doc.name, payment_number)
                
                if not gateway_res.get("status"):
                    frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True)
                    frappe.db.commit()
                    return {
                        "status": False,
                        "message": gateway_res.get("response_massage") or gateway_res.get("message") or "Payment Failed"
                    }
                
                if doc.meta.has_field("payments") and getattr(doc, "is_pos", 0):
                    doc.append("payments", {
                        "mode_of_payment": mode_of_payment,
                        "amount": p.get("amount"),
                        "payment_reference": gateway_res.get("payment_id") or gateway_res.get("transactionId"),
                        "processed_by": frappe.session.user
                    })

                else:
                    payment_entry = create_payment_entry(
                        doc=doc,
                        amount=p.get("amount"),
                        mode_of_payment=mode_of_payment,
                        reference_no=gateway_res.get("payment_id") or gateway_res.get("transactionId")
                    )
                
                if hasattr(doc, "custom_payment_status"):
                    if doc.docstatus == 0:
                        doc.custom_payment_status = "Paid"
                        doc.save(ignore_permissions=True)
                    else:
                        doc.db_set("custom_payment_status", "Paid")

                    
                result["gateway_responses"].append(gateway_res)
                
                # Keep compatibility with old request_payment returning this directly
                if not result["payment_request_details"]:
                    result["payment_request_details"] = gateway_res
                
            
    return result


@frappe.whitelist()
def request_payment(payments=None, sales_invoice=None, data=None):
    """
    Deprecated Wrapper: Converts legacy arguments into the modern `process_payment` payload.
    """
    if not payments:
        payments = []
    
    if isinstance(payments, dict):
        payments = [payments]
        
    payload = {
        "payment_number": data.get("payment_number") or data.get("mobile") if data else None,
        "wallet_amount": 0,
        "loyalty_points": 0,
        "payments": payments
    }
    
    res = process_payment(sales_invoice, payload)
    
    return {
        "payment_request_details": res.get("payment_request_details"), 
        "sales_invoice": sales_invoice
    }


def process_order_payments(sales_order, payment_method, payment_allocation):
    """
    Deprecated Wrapper: Converts legacy arguments into the modern `process_payment` payload.
    """
    use_balance = bool(payment_method.get("balance"))
    use_point = bool(payment_method.get("point"))
    select_payment = payment_method.get("select_payment")
    payment_number = payment_method.get("payment_number")

    payments = []
    if select_payment and payment_allocation.get("amount_with_payment_method", 0) > 0:
        payments.append({
            "mode_of_payment": select_payment,
            "amount": payment_allocation.get("amount_with_payment_method")
        })

    payload = {
        "payment_number": payment_number,
        "wallet_amount": payment_allocation.get("wallet_used", 0) if use_balance else 0,
        "loyalty_points": payment_allocation.get("point_amount", 0) if use_point else 0,
        "payments": payments
    }

    res = process_payment(sales_order, payload)
    
    if not res.get("status"):
        return res
        
    return {"status": True, "redirect_page": "completed"}


@frappe.whitelist(allow_guest=True)
def receive_sms():
    from payments.payments.doctype.sms_inbox.sms_inbox import receive_sms
    data = frappe.request.get_json()
    frappe.local.request.data = json.dumps(data)
    return receive_sms()
    
        
    
  