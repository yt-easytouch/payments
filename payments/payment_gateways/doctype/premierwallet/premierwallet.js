// Copyright (c) 2025, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Premierwallet", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.staging) return;

		frm.add_custom_button(__("Test Login"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the settings first."));
				return;
			}
			frm.call({ method: "test_login", doc: frm.doc, freeze: true }).then(({ message }) => {
				frappe.msgprint({
					title: __("Premier Wallet"),
					message: message.message,
					indicator: message.status ? "green" : "red",
				});
			});
		});
	},
});
