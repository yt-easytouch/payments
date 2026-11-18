# Copyright (c) 2026, Yooltech and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WaafiPayTransaction(Document):
	def on_trash(self):
		frappe.throw(_("Payment transactions cannot be deleted"))


def on_doctype_update():
	frappe.db.add_index("WaafiPay Transaction", ["status", "creation"])
	frappe.db.add_index("WaafiPay Transaction", ["reference_doctype", "reference_name"])
	frappe.db.add_index("WaafiPay Transaction", ["status", "method", "closed_at"])
