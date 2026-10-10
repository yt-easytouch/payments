"""Python API for other apps (not whitelisted): start a gateway payment, re-verify it, settle it once.

Callers register `payment_handlers = {"<Reference DocType>": "dotted.path"}` in their hooks.py; the handler is
called as handler(transaction, event) inside the same DB transaction whenever a transaction changes status,
gets flagged for review, or receives a late approval.

Lock order: the WaafiPay Transaction row is locked first, handlers take their own locks after it. Callers must
never hold their own row locks while calling start/verify/expire_stale, so no path locks in the other order.
"""

import hashlib
import re
import secrets
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, get_url, now_datetime
from frappe.utils.password import get_decrypted_password, remove_encrypted_password

from payments.waafipay_checkout import client
from payments.waafipay_checkout.client import GatewayError

DOCTYPE = "WaafiPay Transaction"
SETTINGS = "WaafiPay Settings"
GATEWAY = "waafipay"
CURRENCY = "USD"
WALLET_METHODS = ("evc_plus", "zaad", "sahal")
METHODS = (*WALLET_METHODS, "card")
OPEN = ("pending", "processing")

RECONCILE_AFTER = timedelta(minutes=1)
UNSENT_AFTER = timedelta(minutes=5)
HPP_REUSE = timedelta(minutes=4)
HPP_TTL = timedelta(minutes=10)
EXPIRE_AFTER = timedelta(minutes=30)
GIVE_UP_AFTER = timedelta(hours=24)
RECHECK_AFTER = timedelta(minutes=30)
RECHECK_WITHIN = timedelta(hours=24)
RETURN_CHECK_EVERY = 10
PHONE_LIMIT = 3
PHONE_WINDOW = 600
WALLET_QUEUE = "payments"

REFERENCE = re.compile(r"^PT[0-9A-F]{24}$")
RESULT_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
SAFE_PATH = re.compile(r"^/(?![/\\])[\x21-\x7e]{0,499}$")
RETURN_ENDPOINT = "/api/method/payments.waafipay_checkout.api_public.waafipay_return"

DECLINED = "The payment was declined."
ABORTED = "The payment was cancelled on the phone."
NO_BALANCE = "There was not enough balance for this payment."
UNAVAILABLE = "Payments are unavailable right now."
EXPIRED = "The payment request expired before it was approved."
NOT_SENT = "The payment request could not be sent."
MISMATCH = "The payment could not be verified. Contact support."

REVIEW_MISMATCH = "WaafiPay approved a payment that does not match this transaction."
REVIEW_EXPIRED = "Expired without a confirmed outcome; the payer may have paid."
REVIEW_LATE = "WaafiPay approved transaction {0} after this payment was {1}."

wallet_phone = client.wallet_phone
mask_phone = client.mask_phone


class PaymentError(Exception):
	def __init__(self, code: str, transaction: str | None = None):
		super().__init__(code)
		self.code = code
		self.transaction = transaction


def now():
	return now_datetime()


def _commit():
	if not frappe.flags.in_test:
		frappe.db.commit()


@contextmanager
def _atomic():
	name = "pt_" + secrets.token_hex(6)
	frappe.db.savepoint(name)
	try:
		yield
	except BaseException:
		frappe.db.rollback(save_point=name)
		raise
	else:
		frappe.db.release_savepoint(name)


def get(name) -> frappe._dict | None:
	return frappe.db.get_value(DOCTYPE, name, "*", as_dict=True) if name else None


def _lock(name) -> frappe._dict | None:
	return frappe.db.get_value(DOCTYPE, name, "*", as_dict=True, for_update=True) if name else None


def _age(row) -> timedelta:
	return now() - get_datetime(row.creation)


def _secret(doctype, name, field) -> str | None:
	return get_decrypted_password(doctype, name, field, raise_exception=False)


def credentials(require_enabled: bool = True) -> client.Credentials | None:
	values = frappe.db.get_singles_dict(SETTINGS)
	if (require_enabled and not cint(values.get("enabled"))) or not values.get("merchant_uid"):
		return None
	return client.Credentials(
		merchant_uid=values.get("merchant_uid"),
		api_user_id=values.get("api_user_id") or None,
		api_key=_secret(SETTINGS, SETTINGS, "api_key"),
		store_id=values.get("store_id") or None,
		hpp_key=_secret(SETTINGS, SETTINGS, "hpp_key"),
		sandbox=cint(values.get("sandbox", 1)) == 1,
	)


def safe_return_path(value) -> str:
	"""Only a same-site absolute path ("/x?y"); anything else (scheme, //host, backslash, whitespace) → "/"."""
	return value if isinstance(value, str) and SAFE_PATH.fullmatch(value) else "/"


def wallet_queue() -> str:
	"""A dedicated "payments" queue when common_site_config defines that worker, else "long"."""
	return WALLET_QUEUE if WALLET_QUEUE in (frappe.conf.get("workers") or {}) else "long"


def _throttled() -> bool:
	return bool(getattr(frappe.local, "request", None) or frappe.flags.payments_test_throttle)


def _once_per(key: str, seconds: int) -> bool:
	if not _throttled():
		return True
	return bool(frappe.cache.set(frappe.cache.make_key(key), 1, ex=seconds, nx=True))


def _phone_key(phone_secret: str) -> str:
	digest = hashlib.sha256(phone_secret.encode()).hexdigest()
	return frappe.cache.make_key(f"payments:wallet_phone:{digest}")


def _phone_attempts(phone_secret: str) -> int:
	if not _throttled():
		return 0
	return int(frappe.cache.get(_phone_key(phone_secret)) or 0)


def _count_phone(phone_secret: str) -> bool:
	if not _throttled():
		return True
	key = _phone_key(phone_secret)
	if not frappe.cache.get(key):
		frappe.cache.setex(key, PHONE_WINDOW, 0)
	return frappe.cache.incrby(key, 1) <= PHONE_LIMIT


def _check(method, phone, currency):
	if method not in METHODS:
		raise PaymentError("invalid_method")
	if str(currency or "").upper() != CURRENCY:
		raise PaymentError("invalid_currency")
	wallet = method in WALLET_METHODS
	phone_secret = wallet_phone(phone) if wallet else None
	if wallet and not phone_secret:
		raise PaymentError("invalid_phone")
	creds = credentials()
	if not creds or not (creds.can_charge_wallet if wallet else creds.can_use_hpp):
		raise PaymentError("gateway_unavailable")
	if phone_secret and _phone_attempts(phone_secret) >= PHONE_LIMIT:
		raise PaymentError("too_many_attempts")
	return creds, phone_secret


def check(method: str, *, phone: str | None = None, currency: str = CURRENCY) -> None:
	"""Validates what start() would refuse, without creating anything; raises PaymentError."""
	_check(method, phone, currency)


def start(
	reference_doctype: str,
	reference_name: str,
	amount_cents: int,
	currency: str,
	method: str,
	*,
	phone: str | None = None,
	return_path: str = "/",
	description: str = "",
	base_url: str | None = None,
) -> frappe._dict:
	"""Creates a WaafiPay Transaction and sends it to WaafiPay; raises PaymentError (with .transaction once one exists).

	Wallets are charged by a background job. Cards commit the open transaction, then open the hosted page;
	the browser comes back through RETURN_ENDPOINT on base_url (default get_url()) and is sent to return_path.
	"""
	creds, phone_secret = _check(method, phone, currency)
	if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
		raise PaymentError("invalid_amount")
	if not reference_doctype or not reference_name or not frappe.db.exists(reference_doctype, reference_name):
		raise PaymentError("invalid_reference")
	if phone_secret and not _count_phone(phone_secret):
		raise PaymentError("too_many_attempts")
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"gateway": GATEWAY,
			"method": method,
			"amount_cents": amount_cents,
			"currency": CURRENCY,
			"status": "pending",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"reference_id": "PT" + secrets.token_hex(12).upper(),
			"payer_phone": phone_secret,
			"payer_phone_masked": mask_phone(phone_secret) if phone_secret else None,
			"return_path": safe_return_path(return_path),
			"description": str(description or "")[:255],
		}
	)
	doc.flags.ignore_permissions = True
	with _atomic():
		doc.insert()
	if phone_secret:
		frappe.enqueue(
			"payments.waafipay_checkout.api.charge_wallet",
			queue=wallet_queue(),
			timeout=300,
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			transaction=doc.name,
		)
		return get(doc.name)
	return _open_hosted_page(doc.name, creds, base_url)


def _callback_url(base_url, reference_id) -> str:
	base = (
		base_url if isinstance(base_url, str) and base_url.startswith(("https://", "http://")) else get_url()
	)
	return f"{base.rstrip('/')}{RETURN_ENDPOINT}?ref={reference_id}"


def _open_hosted_page(name, creds: client.Credentials, base_url) -> frappe._dict:
	row = get(name)
	# the row must survive even if this request dies after WaafiPay has opened the page
	_commit()
	callback = _callback_url(base_url, row.reference_id)
	try:
		result = client.start_hpp(
			creds,
			row.reference_id,
			cint(row.amount_cents),
			row.currency,
			row.description or "",
			callback,
			callback,
		)
	except GatewayError:
		_finish(name, "failed", UNAVAILABLE, expect=("pending",))
		raise PaymentError("gateway_unavailable", name)
	if not result.success or not (result.hpp_url or "").startswith("https://"):
		_finish(name, "failed", DECLINED, result, expect=("pending",))
		raise PaymentError("gateway_declined", name)
	with _atomic():
		locked = _lock(name)
		if locked and locked.status == "pending":
			doc = frappe.get_doc(DOCTYPE, name)
			doc.redirect_url = result.hpp_url
			doc.raw_response = frappe.as_json(result.raw)
			if result.reference_id and result.reference_id != row.reference_id:
				doc.provider_token = result.reference_id
			_save(doc)
	return get(name)


def hosted_page_live(row) -> bool:
	"""The hosted page WaafiPay opened for this card payment can still be used."""
	return bool(
		row
		and row.method == "card"
		and row.status == "pending"
		and row.redirect_url
		and _age(row) < HPP_REUSE
	)


def charge_wallet(transaction: str):
	"""Background job: API_PURCHASE holds the line while the customer approves on the phone."""
	with _atomic():
		row = _lock(transaction)
		if not row or row.status != "pending" or row.method not in WALLET_METHODS:
			return
		_update(row, {"status": "processing"}, "status")
	_commit()
	phone_secret = _secret(DOCTYPE, transaction, "payer_phone")
	remove_encrypted_password(DOCTYPE, transaction, "payer_phone")
	creds = credentials(require_enabled=False)
	if not phone_secret or not creds or not creds.can_charge_wallet:
		_finish(transaction, "failed", UNAVAILABLE, expect=("processing",))
		_commit()
		return
	try:
		result = client.purchase_mwallet(
			creds, row.reference_id, cint(row.amount_cents), row.currency, phone_secret, row.description or ""
		)
	except GatewayError:
		# outcome unknown (the customer may still approve): reconcile() decides
		_commit()
		return
	verify(transaction, evidence=result)
	_commit()


def _same_reference(row, reference) -> bool:
	if not reference:
		return False
	return reference in (row.reference_id, row.provider_token) or reference.endswith(f"_{row.reference_id}")


def _decline_reason(result: client.Result) -> str:
	message = result.message.lower()
	if "4004" in message or "abort" in message or result.code == "5306":
		return ABORTED
	if "8001" in message or "balance" in message:
		return NO_BALANCE
	return DECLINED


def _provider_ref_taken(transaction_id, name) -> bool:
	return bool(frappe.db.exists(DOCTYPE, {"provider_ref": transaction_id, "name": ["!=", name]}))


def _verdict(row, result: client.Result, direct: bool) -> tuple[str | None, str | None]:
	"""direct: the answer to our own API_PURCHASE call; otherwise a lookup we made by reference."""
	same = _same_reference(row, result.reference_id)
	if result.approved:
		if not same:
			return None, None
		if result.currency and result.currency != row.currency:
			return "failed", MISMATCH
		if result.amount is None or result.amount * 100 != cint(row.amount_cents):
			return "failed", MISMATCH
		if result.transaction_id and _provider_ref_taken(result.transaction_id, row.name):
			return "failed", MISMATCH
		return "paid", None
	if direct and (not result.success or result.declined):
		return "failed", _decline_reason(result)
	if same and result.declined:
		return "failed", _decline_reason(result)
	return None, None


def _lookup(row, token=None) -> client.Result | None:
	"""Asks WaafiPay by reference (and by the browser's result token); GatewayError only if no lookup answered."""
	if row.status == "pending" and row.method in WALLET_METHODS:
		return None
	creds = credentials(require_enabled=False)
	if not creds or not creds.can_use_hpp:
		return None
	unreachable = False
	for reference in dict.fromkeys(filter(None, (row.reference_id, row.provider_token))):
		try:
			result = client.get_transaction(creds, reference)
		except GatewayError:
			unreachable = True
			continue
		if result.success and _same_reference(row, result.reference_id):
			return result
	if token and RESULT_TOKEN.fullmatch(token):
		try:
			result = client.get_hpp_result(creds, token)
		except GatewayError:
			unreachable = True
		else:
			if _same_reference(row, result.reference_id):
				return result
	if unreachable:
		raise GatewayError("lookup failed")
	return None


def verify(name, evidence: client.Result | None = None, token: str | None = None) -> frappe._dict | None:
	"""Re-verify with WaafiPay and settle once. Raises GatewayError when WaafiPay cannot be asked."""
	row = get(name)
	if not row:
		return None
	direct = evidence is not None
	if row.status not in OPEN:
		if direct and evidence.approved and _same_reference(row, evidence.reference_id):
			_late(name, evidence)
		return get(name)
	result = evidence if direct else _lookup(row, token)
	if result is None:
		return row
	status, reason = _verdict(row, result, direct)
	if not status:
		return row
	return _finish(name, status, reason, result)


def _save(doc):
	doc.flags.ignore_permissions = True
	doc.save()
	return doc


def _event(kind: str, before, result: client.Result | None) -> frappe._dict:
	return frappe._dict(
		kind=kind,
		previous_status=before.status,
		previous_needs_review=cint(before.needs_review),
		transaction_id=result.transaction_id if result else None,
		amount=str(result.amount) if result and result.amount is not None else None,
		currency=result.currency if result else None,
	)


def _notify(row, event):
	for path in frappe.get_hooks("payment_handlers", {}).get(row.reference_doctype) or []:
		frappe.get_attr(path)(row, event)


def _update(before, values: dict, kind: str, result: client.Result | None = None):
	"""Caller holds the row lock. Saves, then lets the owning app react in the same DB transaction."""
	doc = frappe.get_doc(DOCTYPE, before.name)
	doc.update(values)
	_save(doc)
	_notify(get(before.name), _event(kind, before, result))


def _review_note(row, status: str, reason: str | None) -> str | None:
	if reason == MISMATCH:
		return REVIEW_MISMATCH
	if status == "expired" and (row.status == "processing" or (row.method == "card" and row.redirect_url)):
		return REVIEW_EXPIRED
	return None


def _finish(
	name,
	status: str,
	reason: str | None = None,
	result: client.Result | None = None,
	expect: tuple[str, ...] = OPEN,
):
	"""Moves an open transaction to a final status if it is still in `expect`; otherwise a no-op,
	except that an approval for a row that is no longer open is recorded for review, never dropped."""
	with _atomic():
		row = _lock(name)
		if not row:
			return None
		if row.status not in expect or row.status not in OPEN:
			if result is not None and result.approved and _same_reference(row, result.reference_id):
				_record_late(row, result)
			return get(name)
		values = {
			"status": status,
			"failure_reason": None if status == "paid" else reason,
			"closed_at": now(),
			"payer_phone": None,
		}
		if result is not None:
			values["raw_response"] = frappe.as_json(result.raw)
		if status == "paid":
			values["paid_at"] = now()
			values["provider_ref"] = result.transaction_id if result else None
		review = _review_note(row, status, reason)
		if review:
			values.update(needs_review=1, review_note=review)
		_update(row, values, "status", result)
	return get(name)


def _late(name, result: client.Result):
	with _atomic():
		row = _lock(name)
		if row:
			_record_late(row, result)


def _record_late(row, result: client.Result):
	"""Caller holds the row lock: WaafiPay approved a transaction we had already closed."""
	if row.status in OPEN:
		return
	if result.transaction_id and result.transaction_id == row.provider_ref:
		if row.status == "paid" or cint(row.needs_review):
			return
	elif row.status == "paid" and not result.transaction_id:
		return
	values = {
		"needs_review": 1,
		"review_note": REVIEW_LATE.format(result.transaction_id or "unknown", row.status),
		"raw_response": frappe.as_json(result.raw),
	}
	if (
		result.transaction_id
		and not row.provider_ref
		and not _provider_ref_taken(result.transaction_id, row.name)
	):
		values["provider_ref"] = result.transaction_id
	_update(row, values, "late_approval", result)


def expire_stale(name) -> frappe._dict | None:
	"""A hosted page times out after 5 minutes: verify once more, then let a newer payment replace it."""
	row = verify(name)
	if row and row.status == "pending" and row.method == "card" and _age(row) >= HPP_TTL:
		return _finish(name, "expired", EXPIRED, expect=("pending",))
	return row


def handle_return(ref, token=None) -> str:
	"""The browser coming back from the hosted page: a hint only; returns the same-site path to send it to."""
	ref, _sep, rest = str(ref or "").partition("?")
	# WaafiPay appends "?hppResultToken=" even when the callback URL already has a query string
	if not token and rest.startswith("hppResultToken="):
		token = rest.partition("=")[2]
	row = (
		frappe.db.get_value(DOCTYPE, {"reference_id": ref}, ["name", "status", "return_path"], as_dict=True)
		if REFERENCE.fullmatch(ref)
		else None
	)
	if not row:
		return "/"
	if row.status in OPEN and _once_per(f"payments:return:{row.name}", RETURN_CHECK_EVERY):
		try:
			verify(row.name, token=str(token) if token else None)
		except GatewayError:
			pass
	return safe_return_path(row.return_path)


def _reconcile_one(name):
	try:
		row, reachable = verify(name), True
	except GatewayError:
		row, reachable = get(name), False
	if not row or row.status not in OPEN:
		return
	age = _age(row)
	if row.status == "pending" and row.method in WALLET_METHODS:
		if age >= UNSENT_AFTER:
			_finish(name, "expired", NOT_SENT, expect=("pending",))
	elif (reachable and age >= EXPIRE_AFTER) or age >= GIVE_UP_AFTER:
		_finish(name, "expired", EXPIRED, expect=(row.status,))


def _recheck_expired(name):
	row = get(name)
	if not row or row.status != "expired" or row.rechecked_at:
		return
	try:
		result = _lookup(row)
	except GatewayError:
		return
	if result is not None and result.approved:
		_late(name, result)
	frappe.db.set_value(DOCTYPE, name, "rechecked_at", now(), update_modified=False)


def reconcile(limit: int = 100):
	"""Every 5 minutes: settle or expire open transactions; look once more at card payments that expired."""
	open_rows = frappe.get_all(
		DOCTYPE,
		filters={"status": ["in", list(OPEN)], "creation": ["<", now() - RECONCILE_AFTER]},
		pluck="name",
		order_by="creation asc",
		limit=limit,
	)
	expired_cards = frappe.get_all(
		DOCTYPE,
		filters=[
			["status", "=", "expired"],
			["method", "=", "card"],
			["redirect_url", "is", "set"],
			["rechecked_at", "is", "not set"],
			["closed_at", ">=", now() - RECHECK_WITHIN],
			["closed_at", "<", now() - RECHECK_AFTER],
		],
		pluck="name",
		order_by="closed_at asc",
		limit=limit,
	)
	for job, names in ((_reconcile_one, open_rows), (_recheck_expired, expired_cards)):
		for name in names:
			try:
				job(name)
				_commit()
			except Exception:
				frappe.log_error(title=f"WaafiPay Transaction {name} could not be reconciled")
