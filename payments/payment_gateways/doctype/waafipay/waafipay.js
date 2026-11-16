// Copyright (c) 2026, Yooltech and contributors
// For license information, please see license.txt

frappe.ui.form.on("WaafiPay", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.use_hpp) return;
		frm.add_custom_button(frm.doc.webhook_id ? __("Update Webhook") : __("Register Webhook"), () =>
			frm.call("register_webhook").then(() => {
				frm.reload_doc();
				frappe.show_alert({ message: __("Webhook registered with WaafiPay"), indicator: "green" });
			})
		);
	},
});
