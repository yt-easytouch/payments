import frappe




def create_custom_fields_from_json(custom_fields_map):
    """
    Create custom fields from a JSON structure.
    Supports multiple doctypes, multiple fields.
    """
    for doctype, fields in custom_fields_map.items():
        for field in fields:
            fieldname = field.get("fieldname")

            # Skip if exists
            if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
                continue

          

            field_doc = {
                "doctype": "Custom Field",
                "dt": doctype,
                "fieldname": fieldname,
                "label": field.get("label") ,
                "fieldtype": field.get("fieldtype", "Data"),
                "description": field.get("description"),
                "insert_after": field.get("insert_after"),
                "options": field.get("options"),
                "default": field.get("default"),
                "reqd": field.get("reqd", 0),
                "hidden": field.get("hidden", 0),
                "depends_on": field.get("depends_on"),
            }

            frappe.get_doc(field_doc).insert(ignore_permissions=True)

    frappe.db.commit()


def delete_custom_fields_from_json(custom_fields_map):
    """
    Delete ONLY the custom fields created by this app.
    Safe uninstall logic.
    """
    for doctype, fields in custom_fields_map.items():
        for field in fields:
            fieldname = field.get("fieldname")
            # Find matching Custom Field
            cf = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname})
            if cf:
                frappe.delete_doc("Custom Field", cf, ignore_permissions=True)

    frappe.db.commit()


# -------------------------
# JSON CONFIG
# -------------------------

CUSTOM_FIELDS_JSON = {
    "Mode of Payment": [
        {
            "fieldname": "receiver_info",
            "label": "Receiver Info (Account or USSD)",
            "fieldtype": "Data",
            "description": "Use placeholders to generate dynamic Receiver for USSD:\n{blc} → Invoice Balance",
            "insert_after": "accounts",
        }, {
            "fieldname": "is_online_payment",
            "label": "Is Online Payment",
            "fieldtype": "Check",
            "insert_after": "receiver_info",
        },
        {
            "fieldname": "icon",
            "label": "Icon",
            "fieldtype": "Attach Image",
            "insert_after": "is_online_payment"
        },
        {
            "fieldname": "payment_gateway_account",
            "label": "Payment Gateway Account",
            "fieldtype": "Link",
            "options": "Payment Gateway Account",
            "insert_after": "icon",
        },
        {
            "fieldname": "currency",
            "label": "Currency",
            "fieldtype": "Link",
            "options": "Currency",
            "insert_after": "payment_gateway_account",
        },
        {
            "fieldname": "auto_tax_deducted",
            "label": "Auto Tax Deducted",
            "fieldtype": "Section Break",
            "insert_after": "payment_gateway_account",
        },
        {
            "fieldname": "auto_tax_deducted_1",
            "label": "",
            "fieldtype": "Column Break",
            "insert_after": "auto_tax_deducted",
        },
        {
            "fieldname": "merchant_paid_tax_calculated",
            "label": "Merchant-Paid Tax (Calculated)",
            "fieldtype": "Check",
            "insert_after": "auto_tax_deducted_1",
			"description": "Use this when the merchant has already paid the tax, and it's calculated automatically."
        },
        {
            "fieldname": "auto_calculate_tax_on_shift_close",
            "label": "Auto Calculate Tax on Shift Close",
            "fieldtype": "Check",
            "insert_after": "merchant_paid_tax_calculated",
			"description": "Use this to show or trigger tax calculation at shift close based on collected amounts.",
			"depends_on": "eval:doc.merchant_paid_tax_calculated"
        },
        {
            "fieldname": "auto_tax_deducted_2",
            "label": "",
            "fieldtype": "Column Break",
            "insert_after": "auto_calculate_tax_on_shift_close",
        },
        {
            "fieldname": "post_paid_tax_to_ledger_on_receipt",
            "label": "Post Paid Tax to Ledger on Receipt",
            "fieldtype": "Check",
            "insert_after": "auto_tax_deducted_2",
			"description": "Use this when the tax is paid immediately and should be recorded in the GL at the time of the sale.",
			"depends_on": "eval:doc.merchant_paid_tax_calculated"
        }
    ]
}

# -------------------------
# INSTALL & UNINSTALL HOOKS
# -------------------------

def before_install():
	create_custom_fields_from_json(CUSTOM_FIELDS_JSON)


def before_uninstall():
    delete_custom_fields_from_json(CUSTOM_FIELDS_JSON)