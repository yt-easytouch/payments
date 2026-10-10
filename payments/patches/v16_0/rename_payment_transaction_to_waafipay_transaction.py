import frappe
from frappe.query_builder import Table

OLD = "Payment Transaction"
NEW = "WaafiPay Transaction"


def execute():
	"""Renames our Payment Transaction DocType; another app's DocType of that name is left alone."""
	if frappe.db.get_value("DocType", OLD, "module") != "Payments":
		return

	# renaming a DocType does not move the encrypted fields of its records
	auth = Table("__Auth")
	frappe.qb.update(auth).set(auth.doctype, NEW).where(auth.doctype == OLD).run()

	frappe.rename_doc("DocType", OLD, NEW, force=True, show_alert=False)
