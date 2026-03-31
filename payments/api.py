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
        "Mode of Payment", {"name": mode_of_payment}, "custom_payment_gateway_account"
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
@frappe.whitelist()
def  request_payment(payments=None,sales_invoice=None,data=None):
    payment_request_details = None
    if payments:
        payment_number = data.get("payment_number") or data.get("mobile")
        payment_number = remove_country_code(payment_number)
        for p in payments:
            if p.get("amount") > 0 and payment_number:
                mode_of_payment = p.get("mode_of_payment")
                gateway_account = frappe.get_value("Mode of Payment", mode_of_payment, "custom_payment_gateway_account")
                
                if not gateway_account:
                    payment_res = create_payment_from_invoice(sales_invoice.name, payment_number, mode_of_payment)                        
                    payment_request_details = payment_res
                else:           
                    res = process_payment_gateway(mode_of_payment, p.get("amount"), sales_invoice.name, payment_number)
                    if not res.get("status"):
                        frappe.throw(res.get("message") or "Payment Failed")
                    else:
                        sales_invoice.append("payments", {
                            "mode_of_payment": mode_of_payment,
                            "amount": p.get("amount"),
                            "payment_reference": res.get("payment_id"),
                        })
                        sales_invoice.custom_payment_status = "Paid"
                    payment_request_details = res
    return  {"payment_request_details": payment_request_details, "sales_invoice": sales_invoice}


@frappe.whitelist(allow_guest=True)
def receive_sms():
    from payments.payments.doctype.sms_inbox.sms_inbox import receive_sms
    data = frappe.request.get_json()
    frappe.local.request.data = json.dumps(data)
    return receive_sms()
    
        
    
  