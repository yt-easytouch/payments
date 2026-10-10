# Copyright (c) 2026, Yooltech and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WaafiPaySettings(Document):
	def validate(self):
		for fieldname in ("merchant_uid", "api_user_id", "store_id"):
			if self.get(fieldname):
				self.set(fieldname, self.get(fieldname).strip())
