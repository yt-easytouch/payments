# Payments

A payments app for frappe.

## Installation
1. Install [bench & frappe](https://frappeframework.com/docs/v14/user/en/installation).

2. Once setup is complete, add the payments app to your bench by running
    ```
    $ bench get-app payments
    ```
3. Install the payments app on the required site by running
    ```
    $ bench --site <sitename> install-app payments
    ```

## App Structure & Details
App has 2 modules - Payments and Payment Gateways.

Payment Module contains the Payment Gateway DocType which creates links for the payment gateways and Payment Gateways Module contain all the Payment Gateway (Razorpay, Stripe, Braintree, Paypal, PayTM) DocTypes.

App adds custom fields to Web Form for facilitating payments upon installation and removes them upon uninstallation.

All general utils are stored in [utils](payments/utils) directory. The utils are written in [utils.py](payments/utils/utils.py) and then imported into the [`__init__.py`](payments/utils/__init__.py) file for easier importing/namespacing.

[overrides](payments/overrides) directory has all the overrides for overriding standard frappe code. Currently it overrides WebForm DocType controller as well as a WebForm whitelisted method.

[templates](payments/templates) directory has all the payment gateways' custom checkout pages.

## WaafiPay Checkout

[waafipay_checkout](payments/waafipay_checkout) is a WaafiPay checkout for Frappe apps such as Bilyeeti. It does not need ERPNext. It supports EVC Plus, ZAAD and SAHAL wallets, and cards on the hosted payment page, in USD only.

- **WaafiPay**: the checkout uses the one WaafiPay record that has **Use for Hosted Checkout** set. Wallets need Merchant ID, User ID and Merchant Key. Cards also need Store ID and HPP Key. **Demo** selects the WaafiPay sandbox. Otherwise the checkout sends requests to **Endpoint**, which must start with `https://`. The keys are encrypted. When no record has the setting, new payments are refused with `gateway_unavailable` and open payments cannot be verified.
- **WaafiPay Transaction**: one record for each gateway payment. It links to the record of the caller through `reference_doctype` and `reference_name`.
- `payments.waafipay_checkout.api` is the Python API (not whitelisted): `check`, `start`, `verify`, `get`, `expire_stale`. It raises `PaymentError` with a `code`, and `GatewayError` when WaafiPay cannot be reached.
- WaafiPay sends the browser back to `/api/method/payments.waafipay_checkout.api_public.waafipay_return?ref=<referenceId>`. The payment is verified again before the browser goes to the `return_path` of the transaction.
- Each 5 minutes, `payments.waafipay_checkout.api.reconcile` verifies open transactions, expires abandoned ones and looks again at expired card payments for late approvals.
- Wallet charges run on the `payments` RQ queue when `common_site_config.json` defines it (`"workers": {"payments": {"timeout": 300}}`), otherwise on `long`.

Other apps register a handler for each reference DocType in their `hooks.py`:

```python
payment_handlers = {"Sales Order": "your_app.billing.on_transaction_update"}
```

The handler is called as `handler(transaction, event)` in the same database transaction, with the WaafiPay Transaction row locked, when a transaction changes status, is flagged for review, or gets a late approval. Do not hold your own row locks when you call `start`, `verify` or `expire_stale`.

## Ongoing Work
- New API design: https://github.com/frappe/payments/pull/53
- Mollie Integration: https://github.com/frappe/payments/pull/68 (awaiting the former, but you may use the branc)

## License
MIT ([license.txt](license.txt))
