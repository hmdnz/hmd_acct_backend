import hashlib
import hmac
import json
import logging
from typing import Any
import httpx
from fastapi import HTTPException, status
from app.core.config import settings

logger = logging.getLogger(__name__)

PAYSTACK_API_BASE = "https://api.paystack.co"


def verify_paystack_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Cryptographically verifies the Paystack webhook HMAC-SHA512 signature.
    """
    if not signature_header or not settings.PAYSTACK_SECRET_KEY:
        return False

    computed_hash = hmac.new(
        key=settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(computed_hash, signature_header)


async def verify_transaction(reference: str) -> dict[str, Any]:
    """
    Verifies a transaction directly with Paystack API.
    """
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{PAYSTACK_API_BASE}/transaction/verify/{reference}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            data = resp.json()
            if not data.get("status"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Paystack verification failed: {data.get('message')}",
                )
            return data.get("data", {})
        except httpx.HTTPError as exc:
            logger.error("Paystack connection error during transaction verify: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider connection error during verification",
            )


async def initiate_transfer(
    amount_in_kobo: int, recipient_code: str, reference: str, reason: str = "Seller Payout"
) -> dict[str, Any]:
    """
    Initiates a payout transfer to a seller recipient using Paystack Transfers API.
    """
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{PAYSTACK_API_BASE}/transfer"
    payload = {
        "source": "balance",
        "amount": amount_in_kobo,
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
            if not data.get("status"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Paystack transfer rejected: {data.get('message')}",
                )
            return data.get("data", {})
        except httpx.HTTPError as exc:
            logger.error("Paystack connection error during transfer initiate: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error initiating transfer",
            )
