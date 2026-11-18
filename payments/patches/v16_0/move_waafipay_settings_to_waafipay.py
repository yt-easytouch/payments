import frappe
from frappe.utils import cint
from frappe.utils.password import delete_all_passwords_for, get_decrypted_password

OLD = "WaafiPay Settings"
GATEWAY_NAME = "WaafiPay Checkout"


def execute():
	"""Moves the removed WaafiPay Settings single into a WaafiPay record, then drops the single."""
	values = frappe.db.get_singles_dict(OLD)
	if any(values.get(field) for field in ("merchant_uid", "api_user_id", "store_id")):
		create_checkout_account(values)

	frappe.db.delete("Singles", {"doctype": OLD})
	delete_all_passwords_for(OLD, OLD)
	if frappe.db.get_value("DocType", OLD, "module") == "Payments":
		frappe.delete_doc("DocType", OLD, force=True, ignore_missing=True)


def create_checkout_account(values: frappe._dict) -> None:
	is_ticked_elsewhere = frappe.db.exists("WaafiPay", {"use_for_checkout": 1})
	frappe.get_doc(
		{
			"doctype": "WaafiPay",
			"gateway_name": get_free_gateway_name(),
			"merchant_id": values.get("merchant_uid"),
			"user_id": values.get("api_user_id"),
			"merchant_key": get_decrypted_password(OLD, OLD, "api_key", raise_exception=False),
			"store_id": values.get("store_id"),
			"hpp_key": get_decrypted_password(OLD, OLD, "hpp_key", raise_exception=False),
			"staging": cint(values.get("sandbox", 1)),
			"use_for_checkout": int(cint(values.get("enabled")) and not is_ticked_elsewhere),
		}
	).insert(ignore_permissions=True)


def get_free_gateway_name() -> str:
	name, number = GATEWAY_NAME, 1
	while frappe.db.exists("WaafiPay", name):
		number += 1
		name = f"{GATEWAY_NAME} {number}"
	return name
