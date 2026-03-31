# Copyright (c) 2026, yooltech and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

import re
from datetime import datetime
import frappe
from frappe import _
from frappe.utils import nowdate, flt
import json

class SMSInbox(Document):
	pass


@frappe.whitelist(allow_guest=True)
def insert_sms_inbox(sms_text, sender_number, amount, balance, date_received=None, update_auto=False):
    """
    Inserts an SMS record directly into the SMS Inbox doctype.
    """
    sms_doc = frappe.get_doc({
        "doctype": "SMS Inbox",
        "sender_number": sender_number, 
        "amount": flt(amount),                       
        "date_received": date_received or datetime.now(),        
        "message_content": sms_text, 
        "balance": flt(balance),           
        "status": "Received"          
    })

    sms_doc.insert(ignore_permissions=True)  
    frappe.db.commit() 
    
    if update_auto:              
        pay_payment_request_sms(sms_inbox_name=sms_doc.name)
        
    return sms_doc.name


def parse_hormuud_evc_sms(sms_text):
    """
    Parses a Hormuud EVC Plus message and returns a dictionary of data.
    """
    amount_match = re.search(r"waxaad \$(\d+(?:\.\d+)?)", sms_text, re.IGNORECASE)
    amount = flt(amount_match.group(1)) if amount_match else 0.0
    
    sender_match = re.search(r"heshay (\d+)", sms_text)
    sender_number = sender_match.group(1) if sender_match else ""

    balance_match = re.search(r"haraagagu waa \$(\d+(?:\.\d+)?)", sms_text, re.IGNORECASE)    
    balance = flt(balance_match.group(1)) if balance_match else 0.0

    date_match = re.search(r"Tar: (\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", sms_text)
    if date_match:
        try:
            date_received = datetime.strptime(date_match.group(1), "%y/%m/%d %H:%M:%S")
        except Exception:
            date_received = datetime.now()
    else:
        date_received = datetime.now()
        
    return {
        "amount": amount,
        "sender_number": sender_number,
        "balance": balance,
        "date_received": date_received
    }


@frappe.whitelist(allow_guest=True)
def receive_sms():
    data = frappe.request.get_json()
    if not data:
        return {"status": "error", "message": "No data received"}
    
    body = data.get("body", "")
    sender = data.get("from", "") # Gateway sender (e.g., '192')
    
    # We only care about Hormuud EVC Plus messages from '192' that contain 'waxaad $'
    if sender == "192" and "waxaad $" in body:
        # Parse the message
        parsed_data = parse_hormuud_evc_sms(body)
        
        # Insert into SMS Inbox
        sms_name = insert_sms_inbox(
            sms_text=body,
            sender_number=parsed_data["sender_number"],
            amount=parsed_data["amount"],
            balance=parsed_data["balance"],
            date_received=parsed_data["date_received"],
            update_auto=True
        )
        return {"status": "success", "sms_name": sms_name}
    else:
        return {"status": "ignored", "message": "Message does not match criteria"}



# @frappe.whitelist(allow_guest=True)
# def get_all_sms_inbox():
#     sms_docs = frappe.get_list("SMS Inbox", 
#     fields=["name", "sender_number", "sender_number", "amount", "date_received", "message_content", "balance", "status"],
#     ignore_permissions=True)
#     return sms_docs




@frappe.whitelist(allow_guest=True)
def pay_payment_request_sms(sms_inbox_name=None, payment_request_name=None):
    if not sms_inbox_name and not payment_request_name:
        return {
            "status": "error", 
            "message": "Please provide either sms_inbox_name or payment_request_name",
            "error_code": "MISSING_PARAMETERS"
        }
    
  
    if sms_inbox_name and not payment_request_name:
        sms_inbox = frappe.get_value(
            "SMS Inbox", 
            sms_inbox_name, 
            ["status", "sender_number", "amount", "message_content", "name"],
            as_dict=True
        )
        
        if not sms_inbox:
            return {
                "status": "error", 
                "message": f"SMS Inbox {sms_inbox_name} not found",
                "error_code": "SMS_NOT_FOUND"
            }
        
        if sms_inbox.get("status") == "Read":
            return {
                "status": "error", 
                "message": f"SMS Inbox {sms_inbox_name} already used (Status: {sms_inbox.get('status')})",
                "error_code": "SMS_ALREADY_USED",
                "sms_status": sms_inbox.get("status")
            }
        
        payment_request_name = frappe.get_value(
            "Payment Request", 
            {
                "phone_number": sms_inbox.get('sender_number'),
                "grand_total": sms_inbox.get("amount"),
                "status": ["in", ["Requested", "Initiated"]],
                "docstatus": 1
            },
            "name",
            order_by="creation DESC"
        )
            
        if not payment_request_name:
            return {
                "status": "error", 
                "message": f"No pending Payment Request found for sender {sms_inbox.get('sender_number')} or amount {sms_inbox.get('amount')}",
                "error_code": "PR_NOT_FOUND"
            }
    
   
    if payment_request_name and not sms_inbox_name:
        payment_request = frappe.get_value(
            "Payment Request", 
            payment_request_name,
            ["status", "phone_number", "name", "grand_total", "reference_name"],
            as_dict=True
        )
        
        if not payment_request:
            return {
                "status": "error", 
                "message": f"Payment Request {payment_request_name} not found",
                "error_code": "PR_NOT_FOUND"
            }
        
        if payment_request.get("status") == "Paid":
            return {
                "status": "error", 
                "message": f"Payment Request {payment_request_name} is already paid",
                "error_code": "PR_ALREADY_PAID",
                "pr_status": payment_request.get("status")
            }
        
        sms_filters = {"status": "Received"}
        
        if payment_request.get("phone_number"):
            sms_filters["sender_number"] = payment_request.get('phone_number')
        
        sms_inbox_name = frappe.get_value(
            "SMS Inbox",
            sms_filters,
            "name",
            order_by="creation DESC"
        )
        
        if not sms_inbox_name:
            return {
                "status": "error", 
                "message": f"No unread SMS Inbox found for Payment Request {payment_request_name}",
                "error_code": "SMS_NOT_FOUND"
            }
        
        # Get SMS details
        sms_inbox = frappe.get_value(
            "SMS Inbox", 
            sms_inbox_name, 
            ["status", "sender_number", "amount", "message_content", "name"],
            as_dict=True
        )
    
  
    if not payment_request_name or not sms_inbox_name:
        return {
            "status": "error", 
            "message": "Could not establish link between SMS and Payment Request",
            "error_code": "LINK_FAILED"
        }
    
    # Get fresh copies of both documents
    payment_request = frappe.get_doc("Payment Request", payment_request_name)
    sms_inbox = frappe.get_doc("SMS Inbox", sms_inbox_name)
    
    if payment_request.docstatus != 1:
        return {
            "status": "error", 
            "message": f"Payment Request {payment_request_name} is not submitted (Status: {payment_request.status})",
            "error_code": "PR_NOT_SUBMITTED"
        }
    
    # Verify SMS is not already used
    if sms_inbox.status == "Read":
        return {
            "status": "error", 
            "message": f"SMS Inbox {sms_inbox_name} already used (Status: {sms_inbox.status})",
            "error_code": "SMS_ALREADY_USED"
        }
    
    if abs(sms_inbox.amount - payment_request.grand_total) != 0:
        return {
            "status": "error", 
            "message": f"Amount mismatch: SMS amount {sms_inbox.amount} vs Payment Request amount {payment_request.grand_total}",
            "error_code": "AMOUNT_MISMATCH",
            "sms_amount": sms_inbox.amount,
            "pr_amount": payment_request.grand_total
        }
    
    try:
     
        
        payment_entry = create_payment_entry_from_sms(payment_request, sms_inbox)
        
        frappe.db.set_value("Payment Request", payment_request_name, {
            "status": "Paid"
        })
        
        # 5.3 Update SMS Inbox status
        frappe.db.set_value("SMS Inbox", sms_inbox_name, {
            "status": "Read"
        })
        
        
        
        frappe.db.commit()        
        return {
            "status": "success", 
            "message": "Payment processed successfully",
            "payment_request": payment_request_name,
            "sms_inbox": sms_inbox_name,
            "payment_entry": payment_entry.name if payment_entry else None,
            "amount": payment_request.grand_total,
            "currency": payment_request.currency,
            "customer": payment_request.party_name
        }
        
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(
            f"Error processing SMS payment: PR={payment_request_name}, SMS={sms_inbox_name}, Error={str(e)}",
            "SMS Payment Error"
        )
        return {
            "status": "error", 
            "message": f"Payment processing failed: {str(e)}",
            "error_code": "PROCESSING_ERROR"
        }


def create_payment_entry_from_sms(payment_request, sms_inbox):
    try:
        status = frappe.get_value("Sales Invoice", payment_request.reference_name, "docstatus")
        if status == 1:
            
            from erpnext.accounts.doctype.payment_request.payment_request import make_payment_entry
            
            # Create Payment Entry with correct accounts populated
            pe = make_payment_entry(payment_request.name)
            
            pe.reference_no = sms_inbox.name
            pe.reference_date = sms_inbox.date_received or nowdate()
            pe.remarks = f"Payment via SMS: {sms_inbox.message_content[:100]}"
            
            pe.insert(ignore_permissions=True)
            pe.submit()
            
            return pe
        else:
            update_sales_invoice_payment_status(payment_request, sms_inbox)
            return None
    except Exception as e:
        frappe.log_error(f"Payment Entry creation failed: {str(e)}", "SMS Payment Entry Error")
        raise e


def update_sales_invoice_payment_status(payment_request, sms_inbox):
    try:
        si = frappe.get_doc("Sales Invoice", payment_request.reference_name)
        
        si.append("payments", {
                    "mode_of_payment": payment_request.mode_of_payment,
                    "processed_by": frappe.session.user,
                    "amount": si.grand_total,
                    "reference_no": sms_inbox.name
                })
        si.add_comment(
            "Info", 
            f"Payment received via SMS: {sms_inbox.message_content[:200]}"
        )
        si.save(ignore_permissions=True)
        
        return True
    except Exception as e:
        frappe.log_error(f"Error updating SI {payment_request.reference_name}: {str(e)}", "SMS Payment Status Error")
        raise e


@frappe.whitelist(allow_guest=True)
def check_payment_status(sms_inbox_name=None, payment_request_name=None):
    """
    Check the status of a payment without processing it
    """
    result = {"status": "pending"}
    
    if sms_inbox_name:
        sms = frappe.get_value(
            "SMS Inbox", 
            sms_inbox_name, 
            ["status", "sender_number", "amount", "reference_payment_request"],
            as_dict=True
        )
        if sms:
            result["sms_status"] = sms.status
            result["sender"] = sms.sender_number
            result["amount"] = sms.amount
            if sms.reference_payment_request:
                result["linked_pr"] = sms.reference_payment_request
    
    if payment_request_name:
        pr = frappe.get_value(
            "Payment Request", 
            payment_request_name, 
            ["status", "grand_total", "payment_entry_reference"],
            as_dict=True
        )
        if pr:
            result["pr_status"] = pr.status
            result["pr_amount"] = pr.grand_total
            if pr.payment_entry_reference:
                result["payment_entry"] = pr.payment_entry_reference
    
    return result


