"""The door hamads-dev knocks on, plus the party (seller / rider) directory for the admin screens."""
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.modules.accounting import ingest
from app.modules.accounting.models import Account, JournalEntry, JournalEntryStatus, JournalLine, Party
from app.modules.auth.dependencies import get_current_admin
from app.modules.auth.models import Admin

router = APIRouter(tags=["accounting-bridge"])


@router.post("/accounting/webhooks/ecommerce")
async def ecommerce_webhook(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    db: AsyncSession = Depends(get_db),
):
    raw = await request.body()
    expected = hmac.new(settings.ACCOUNTING_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not x_signature_256 or not hmac.compare_digest(expected, x_signature_256):
        raise HTTPException(401, "Invalid signature")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise HTTPException(400, "Body is not valid JSON")
    return await ingest.ingest_event(
        db, envelope.get("event_key", ""), envelope.get("event_type", ""), envelope.get("data") or {},
    )


@router.get("/admin/accounting/parties")
async def list_parties(
    party_type: str | None = Query(default=None, pattern="^(seller|rider)$"),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    """Sellers and riders the ledger has seen, with what Hamads currently owes each (2030 for sellers, 2040 for riders)."""
    stmt = select(Party).order_by(Party.name)
    if party_type:
        stmt = stmt.where(Party.party_type == party_type)
    parties = (await db.execute(stmt)).scalars().all()

    balances = {
        (code, sid): credit - debit
        for code, sid, debit, credit in (await db.execute(
            select(Account.code, JournalLine.sub_ledger_id, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
            .where(Account.code.in_(("2030", "2040")), JournalEntry.status == JournalEntryStatus.POSTED)
            .group_by(Account.code, JournalLine.sub_ledger_id)
        )).all()
    }
    return [
        {
            "party_type": p.party_type, "id": p.external_id, "name": p.name,
            "amount_owed": balances.get(("2030" if p.party_type == "seller" else "2040", p.external_id), 0),
        }
        for p in parties
    ]
