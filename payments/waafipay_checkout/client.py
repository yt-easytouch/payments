"""WaafiPay HTTP client (docs.waafipay.com). Pure: no DocType access, never logs keys or full phone numbers."""

import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import requests

SANDBOX_URL = "https://sandbox.waafipay.com/asm"
LIVE_URL = "https://api.waafipay.net/asm"
SUCCESS = "2001"
APPROVED = "APPROVED"
DECLINED_STATES = frozenset({"DECLINED", "REJECTED", "FAILED", "CANCELED", "CANCELLED", "EXPIRED", "TIMEOUT"})
# the customer approves API_PURCHASE on the handset while the call is open
WALLET_TIMEOUT = (10, 95)
TIMEOUT = (10, 20)
RESULT_TOKEN_FORMAT = 4

SECRET_KEYS = frozenset(
	{"apikey", "hppkey", "apiuserid", "merchantuid", "storeid", "hppresulttoken", "secret"}
)
PHONE_KEYS = frozenset({"accountno", "payerid", "subscriptionid", "customer_identity", "msisdn", "phone"})
PHONE_IN_TEXT = re.compile(r"(?<!\d)25[23]\d{7,10}(?!\d)")


class GatewayError(Exception):
	"""WaafiPay could not be reached or answered with something that is not a WaafiPay response."""


@dataclass(frozen=True)
class Credentials:
	merchant_uid: str
	api_user_id: str | None = None
	api_key: str | None = field(default=None, repr=False)
	store_id: str | None = None
	hpp_key: str | None = field(default=None, repr=False)
	sandbox: bool = True

	@property
	def url(self) -> str:
		return SANDBOX_URL if self.sandbox else LIVE_URL

	@property
	def can_charge_wallet(self) -> bool:
		return bool(self.merchant_uid and self.api_user_id and self.api_key)

	@property
	def can_use_hpp(self) -> bool:
		return bool(self.merchant_uid and self.store_id and self.hpp_key)


@dataclass(frozen=True)
class Result:
	code: str
	message: str
	state: str | None
	transaction_id: str | None
	reference_id: str | None
	amount: Decimal | None
	currency: str | None
	hpp_url: str | None
	raw: dict

	@property
	def success(self) -> bool:
		return self.code == SUCCESS

	@property
	def approved(self) -> bool:
		return self.success and self.state == APPROVED

	@property
	def declined(self) -> bool:
		return self.state in DECLINED_STATES


def wallet_phone(value) -> str | None:
	"""Somali mobile number as WaafiPay expects it: 252 and nine digits, no plus."""
	if not isinstance(value, str) or len(value) > 32:
		return None
	digits = re.sub(r"[\s().+-]", "", value)
	if not re.fullmatch(r"[0-9]+", digits):
		return None
	if digits.startswith("00"):
		digits = digits[2:]
	if digits.startswith("252"):
		digits = digits[3:]
	elif digits.startswith("0"):
		digits = digits[1:]
	return f"252{digits}" if re.fullmatch(r"[679][0-9]{8}", digits) else None


def mask_phone(value) -> str:
	digits = re.sub(r"\D", "", str(value or ""))
	if len(digits) < 7:
		return "***"
	return f"{digits[:5]}{'*' * (len(digits) - 7)}{digits[-2:]}"


def scrub(value):
	"""Copy of a request/response with credentials removed and phone numbers masked."""
	if isinstance(value, dict):
		out = {}
		for key, item in value.items():
			lowered = str(key).lower()
			if lowered in SECRET_KEYS:
				continue
			out[key] = mask_phone(item) if lowered in PHONE_KEYS and item else scrub(item)
		return out
	if isinstance(value, list):
		return [scrub(item) for item in value]
	if isinstance(value, str):
		return PHONE_IN_TEXT.sub(lambda m: mask_phone(m.group()), value[:500])
	return value


def amount_text(cents: int) -> str:
	return f"{cents // 100}.{cents % 100:02d}"


def _decimal(value) -> Decimal | None:
	if value is None or value == "":
		return None
	try:
		return Decimal(str(value)).quantize(Decimal("0.01"))
	except (InvalidOperation, ValueError):
		return None


def _text(value) -> str | None:
	return None if value is None or value == "" else str(value).strip()


def _parse(body) -> Result:
	if not isinstance(body, dict) or "responseCode" not in body:
		raise GatewayError("unexpected response")
	params = body.get("params") if isinstance(body.get("params"), dict) else {}
	state = _text(params.get("state") or params.get("status") or params.get("tranStatusDesc"))
	return Result(
		code=str(body.get("responseCode")),
		message=str(body.get("responseMsg") or "")[:500],
		state=state.upper() if state else None,
		transaction_id=_text(params.get("transactionId")),
		reference_id=_text(params.get("referenceId")),
		amount=_decimal(params.get("txAmount") or params.get("tranAmount") or params.get("amount")),
		currency=(_text(params.get("currency")) or "").upper() or None,
		hpp_url=_text(params.get("hppUrl")),
		raw=scrub(body),
	)


def _call(creds: Credentials, service: str, params: dict, timeout) -> Result:
	payload = {
		"schemaVersion": "1.0",
		"requestId": uuid.uuid4().hex,
		"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
		"channelName": "WEB",
		"serviceName": service,
		"serviceParams": params,
	}
	try:
		response = requests.post(creds.url, json=payload, timeout=timeout, allow_redirects=False)
	except requests.RequestException as exc:
		raise GatewayError(type(exc).__name__) from None
	if response.status_code >= 500:
		raise GatewayError(f"http {response.status_code}")
	try:
		body = response.json()
	except ValueError:
		raise GatewayError("invalid json") from None
	return _parse(body)


def _hpp_auth(creds: Credentials) -> dict:
	return {"merchantUid": creds.merchant_uid, "storeId": creds.store_id, "hppKey": creds.hpp_key}


def purchase_mwallet(
	creds: Credentials,
	reference_id: str,
	amount_cents: int,
	currency: str,
	phone_secret: str,
	description: str,
) -> Result:
	"""API_PURCHASE: blocks until the customer approves or rejects on the phone."""
	return _call(
		creds,
		"API_PURCHASE",
		{
			"merchantUid": creds.merchant_uid,
			"apiUserId": creds.api_user_id,
			"apiKey": creds.api_key,
			"paymentMethod": "MWALLET_ACCOUNT",
			"payerInfo": {"accountNo": phone_secret},
			"transactionInfo": {
				"referenceId": reference_id,
				"invoiceId": reference_id,
				"amount": amount_text(amount_cents),
				"currency": currency,
				"description": description[:255],
			},
		},
		WALLET_TIMEOUT,
	)


def start_hpp(
	creds: Credentials,
	reference_id: str,
	amount_cents: int,
	currency: str,
	description: str,
	success_url: str,
	failure_url: str,
) -> Result:
	"""HPP_PURCHASE for cards; the browser comes back to the callback URLs with ?hppResultToken=…"""
	return _call(
		creds,
		"HPP_PURCHASE",
		{
			**_hpp_auth(creds),
			"paymentMethod": "CREDIT_CARD",
			"hppSuccessCallbackUrl": success_url,
			"hppFailureCallbackUrl": failure_url,
			"hppRespDataFormat": RESULT_TOKEN_FORMAT,
			"transactionInfo": {
				"referenceId": reference_id,
				"invoiceId": reference_id,
				"amount": amount_text(amount_cents),
				"currency": currency,
				"description": description[:255],
			},
		},
		TIMEOUT,
	)


def get_transaction(creds: Credentials, reference_id: str) -> Result:
	"""HPP_GETTRANINFO: the transaction WaafiPay holds for our referenceId."""
	return _call(creds, "HPP_GETTRANINFO", {**_hpp_auth(creds), "referenceId": reference_id}, TIMEOUT)


def get_hpp_result(creds: Credentials, result_token: str) -> Result:
	"""HPP_GETRESULTINFO: resolves the hppResultToken the browser brought back."""
	return _call(creds, "HPP_GETRESULTINFO", {**_hpp_auth(creds), "hppResultToken": result_token}, TIMEOUT)
