import re

import frappe
from frappe.utils import now_datetime

from payments.api import remove_country_code


def normalize_number(phone):
	return re.sub(r"\D", "", remove_country_code(phone or ""))


def get_payment_number(customer, mode_of_payment):
	if not (customer and mode_of_payment):
		return None
	return frappe.db.get_value(
		"Customer Payment Number",
		{"parent": customer, "parenttype": "Customer", "mode_of_payment": mode_of_payment},
		"phone_number",
	)


def record_payment_number(customer, mode_of_payment, phone):
	phone = normalize_number(phone)
	if not (customer and mode_of_payment and phone):
		return

	now = now_datetime()
	row = frappe.db.get_value(
		"Customer Payment Number",
		{"parent": customer, "parenttype": "Customer", "mode_of_payment": mode_of_payment},
		"name",
	)
	if row:
		frappe.db.set_value(
			"Customer Payment Number", row, {"phone_number": phone, "last_used_on": now}, update_modified=False
		)
		return

	doc = frappe.get_doc("Customer", customer)
	doc.append("custom_payment_numbers", {"mode_of_payment": mode_of_payment, "phone_number": phone, "last_used_on": now})
	doc.flags.ignore_permissions = True
	doc.save()
