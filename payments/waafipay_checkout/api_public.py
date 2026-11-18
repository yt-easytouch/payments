import html

import frappe
from frappe.rate_limiter import rate_limit
from werkzeug.wrappers import Response

from payments.waafipay_checkout import api


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@rate_limit(limit=30, seconds=600)
def waafipay_return(ref: str | None = None, hppResultToken: str | None = None):
	"""WaafiPay's browser redirect after the hosted page: only a hint, the payment is re-verified."""
	frappe.local.flags.commit = True
	location = html.escape(api.handle_return(ref, hppResultToken), quote=True)
	# WaafiPay requires its callback URLs to answer 200, so this is a page that moves on, not a 302
	page = (
		'<!doctype html><meta charset="utf-8">'
		f'<meta http-equiv="refresh" content="0;url={location}">'
		f'<title>Payment</title><a href="{location}">Continue</a>'
	)
	response = Response(page, status=200, mimetype="text/html")
	response.headers["Cache-Control"] = "no-store"
	response.headers["Referrer-Policy"] = "no-referrer"
	return response
