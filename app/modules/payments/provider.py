import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
import httpx
from app.core.config import settings
from app.modules.payments.models import PaymentProvider

TIMEOUT = 30.0

# Words the providers use for "the customer paid" / "the customer did not pay".
SUCCESS_STATUSES = {"success", "successful", "completed", "paid"}
FAILED_STATUSES = {"failed", "abandoned", "cancelled", "canceled", "declined", "reversed", "expired"}


class ProviderError(RuntimeError):
    """The provider refused or could not complete a call. `status_code` is the HTTP status when there was one."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PaymentProviderClient:
    def __init__(self, provider: PaymentProvider):
        self.provider = provider
        self.base = "https://api.paystack.co" if provider == PaymentProvider.PAYSTACK else settings.SQUAD_BASE_URL.rstrip("/")
        self.secret = settings.PAYSTACK_SECRET_KEY if provider == PaymentProvider.PAYSTACK else settings.SQUAD_SECRET_KEY
        if not self.secret:
            raise ProviderError(f"{provider.value} is not configured")

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(method, f"{self.base}{path}", json=payload, params=params, headers=self.headers())
            try:
                data = response.json()
            except Exception:
                data = {"raw_text": response.text}
            if response.status_code >= 400:
                raise ProviderError(f"provider HTTP {response.status_code}: {data}", response.status_code)
            return data

    async def initialize(self, *, email: str, amount_minor: int, reference: str, callback_url: str | None, currency: str, metadata: dict) -> dict[str, Any]:
        if self.provider == PaymentProvider.PAYSTACK:
            payload = {"email": email, "amount": amount_minor, "currency": currency, "reference": reference, "channels": ["card", "bank", "ussd", "bank_transfer"], "metadata": metadata}
            if callback_url:
                payload["callback_url"] = callback_url
            return await self._request("POST", "/transaction/initialize", payload)
        payload = {"email": email, "amount": amount_minor, "currency": currency, "transaction_ref": reference, "initiate_type": "inline", "payment_channels": ["card", "bank", "ussd", "transfer"]}
        if callback_url:
            payload["callback_url"] = callback_url
        return await self._request("POST", "/transaction/initiate", payload)

    async def verify(self, reference: str) -> dict[str, Any]:
        return await self._request("GET", f"/transaction/verify/{reference}")

    async def refund(self, reference: str, amount_minor: int | None, reason: str, gateway_ref: str | None = None) -> dict[str, Any]:
        if self.provider == PaymentProvider.PAYSTACK:
            payload = {"transaction": reference, "customer_note": reason, "merchant_note": reason}
            if amount_minor is not None:
                payload["amount"] = amount_minor
            return await self._request("POST", "/refund", payload)
        # Squad's refund API (docs.squadco.com/Others/refund-api): refund_type is "Full" or "Partial";
        # refund_amount (kobo) is only sent for partial refunds; gateway_transaction_ref comes from the payment notification.
        payload = {"gateway_transaction_ref": gateway_ref or reference, "transaction_ref": reference,
                   "refund_type": "Partial" if amount_minor else "Full", "reason_for_refund": reason}
        if amount_minor:
            payload["refund_amount"] = str(amount_minor)  # Squad's sample sends it as text
        return await self._request("POST", "/transaction/refund", payload)

    # ---- bank accounts and payouts (Paystack only) ----

    def _paystack_only(self, what: str) -> None:
        if self.provider != PaymentProvider.PAYSTACK:
            raise ProviderError(f"{what} is only supported through Paystack")

    async def list_banks(self) -> list[dict[str, Any]]:
        self._paystack_only("Bank list")
        response = await self._request("GET", "/bank", params={"country": "nigeria", "currency": "NGN", "perPage": 100, "use_cursor": "false"})
        return response.get("data") or []

    async def resolve_account(self, *, account_number: str, bank_code: str) -> str:
        """Ask the bank who owns this account number. Returns the account name.
        Paystack and Squad use different bank codes: pass the code that belongs to THIS provider."""
        if self.provider == PaymentProvider.SQUAD:
            response = await self._request("POST", "/payout/account/lookup", {"bank_code": bank_code, "account_number": account_number})
        else:
            response = await self._request("GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code})
        name = (response.get("data") or {}).get("account_name")
        if not name:
            raise ProviderError("The bank did not return an account name for this account number")
        return str(name)

    async def create_recipient(self, *, name: str, account_number: str, bank_code: str, currency: str) -> dict[str, Any]:
        self._paystack_only("Payout recipient setup")
        return await self._request("POST", "/transferrecipient", {"type": "nuban", "name": name, "account_number": account_number, "bank_code": bank_code, "currency": currency})

    async def transfer(self, *, amount_minor: int, recipient_code: str, reason: str, reference: str) -> dict[str, Any]:
        # Paystack wants transfer references in lowercase letters, digits, "-" and "_".
        self._paystack_only("Payout transfer")
        return await self._request("POST", "/transfer", {"source": "balance", "amount": amount_minor, "recipient": recipient_code, "reason": reason, "reference": reference.lower()})


    # ---- Squad payouts (Squad's "Transfer API": money moves from your Squad ledger to a bank account) ----

    def _squad_only(self, what: str) -> None:
        if self.provider != PaymentProvider.SQUAD:
            raise ProviderError(f"{what} is only for Squad")

    async def squad_transfer(
        self, *, amount_minor: int, bank_code: str, account_number: str, account_name: str, reference: str, remark: str,
    ) -> dict[str, Any]:
        """POST /payout/transfer. `reference` must already start with your Squad merchant ID (SQUAD_MERCHANT_ID_...)."""
        self._squad_only("Squad transfer")
        amount = amount_minor if settings.SQUAD_TRANSFER_AMOUNT_IN_KOBO else amount_minor // 100
        return await self._request("POST", "/payout/transfer", {
            "remark": remark[:100], "bank_code": bank_code, "currency_id": "NGN", "amount": str(amount),
            "account_number": account_number, "transaction_reference": reference, "account_name": account_name,
        })

    async def squad_requery(self, reference: str) -> dict[str, Any]:
        """POST /payout/requery: what happened to a transfer we sent (success, failed, reversed or pending)."""
        self._squad_only("Squad requery")
        return await self._request("POST", "/payout/requery", {"transaction_reference": reference})

    async def balance(self) -> int | None:
        """Money available for payouts, in kobo, or None if the provider's answer cannot be read.
        Used only to avoid sending a transfer that would fail for lack of funds."""
        try:
            if self.provider == PaymentProvider.PAYSTACK:
                rows = (await self._request("GET", "/balance")).get("data") or []
                naira = [r for r in rows if str(r.get("currency", "")).upper() == "NGN"]
                return int(naira[0]["balance"]) if naira else None
            data = (await self._request("GET", "/merchant/balance")).get("data")
            if isinstance(data, dict):
                for key in ("balance", "ledger_balance", "available_balance"):
                    if data.get(key) is not None:
                        return int(Decimal(str(data[key])))
            elif isinstance(data, (int, float, str)):
                return int(Decimal(str(data)))
        except Exception:
            return None
        return None


def verify_webhook_signature(provider: PaymentProvider, raw_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    secret = settings.PAYSTACK_SECRET_KEY if provider == PaymentProvider.PAYSTACK else settings.SQUAD_SECRET_KEY
    if not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    # Squad sends the hash in upper case, Paystack in lower case: compare without caring.
    return hmac.compare_digest(digest.lower(), signature.strip().lower())


def extract_event(provider: PaymentProvider, payload: dict[str, Any]) -> tuple[str, str | None, str]:
    """Returns (event_type, our_reference, unique_event_id).

    Paystack: {"event": "charge.success", "data": {"id": 123, "reference": "..."}}
    Squad:    {"Event": "charge_successful", "TransactionRef": "...", "Body": {...}}
    The event id includes the event type, so a charge and a later refund on the same reference are not
    mistaken for duplicates of each other.
    """
    event = str(payload.get("event") or payload.get("Event") or payload.get("type") or "unknown")
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("Body")
    data = inner if isinstance(inner, dict) else payload
    reference = data.get("reference") or data.get("transaction_ref") or data.get("gateway_transaction_ref") or payload.get("TransactionRef")
    data_id = data.get("id")
    base = str(data_id) if data_id is not None else (str(reference) if reference else hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
    return event, (str(reference) if reference else None), f"{event}:{base}"
