"""Turns signed events from hamads-dev into balanced journal entries.

MODEL: agent / escrow-at-delivery (IFRS 15, marketplace acting as agent)
    customer pays        Dr 1020 Gateway clearing        Cr 2010 Escrow (per seller)   Cr 2020 Delivery fee held
    order delivered      Dr 2010 Escrow                  Cr 2030 Seller payable        Cr 4010 Commission revenue
    delivery completed   Dr 2020 Delivery fee held       Cr 4020 Delivery revenue
    rider earns          Dr 5020 Rider cost              Cr 2040 Rider payable
    seller payout sent   Dr 2030 Seller payable          Cr 1020 Gateway clearing
    rider payout sent    Dr 2040 Rider payable           Cr 1020 Gateway clearing
    refund               Cr 1020 Gateway clearing        Dr 2010 (not delivered) | Dr 2030 + Dr 4090 (delivered) | Dr 2020/4090 (delivery fee)
Gateway -> bank settlements and gateway fees are NOT sent by hamads-dev: enter them with
POST /admin/accounting/events/gateway-settlement from the Paystack/Squad statements.

All amounts in events are KOBO integers. They are converted to Naira once, here.
Every event_key becomes the journal entry's idempotency_key, so a retried event never posts twice.
"""
from decimal import Decimal
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting import service
from app.modules.accounting.models import Party, SubLedgerType, TransactionType
from app.modules.accounting.schemas import JournalEntryCreate, JournalLineIn

VENDOR, RIDER, GATEWAY, NONE = SubLedgerType.VENDOR, SubLedgerType.RIDER, SubLedgerType.GATEWAY, SubLedgerType.NONE


def N(kobo: Any) -> Decimal:
    return (Decimal(int(kobo)) / Decimal(100)).quantize(Decimal("0.01"))


def _add(lines: list[JournalLineIn], code: str, *, dr: int = 0, cr: int = 0,
         sl: SubLedgerType = NONE, sid: str | None = None, desc: str | None = None) -> None:
    """Append a line, skipping zero amounts (the ledger rejects empty lines)."""
    dr, cr = int(dr), int(cr)
    if dr == 0 and cr == 0:
        return
    lines.append(JournalLineIn(
        account_code=code, debit=N(dr), credit=N(cr), sub_ledger_type=sl,
        sub_ledger_id=str(sid) if sid is not None else None, description=(desc or "")[:255] or None,
    ))


def _entry(key: str, ttype: TransactionType, ref: str, narration: str, lines: list[JournalLineIn]) -> JournalEntryCreate:
    return JournalEntryCreate(
        transaction_type=ttype, reference_id=ref, idempotency_key=key, narration=narration[:500], lines=lines,
    )


# ---------------------------------------------------------------------------
# one handler per event type
# ---------------------------------------------------------------------------

def _payment_captured(key: str, d: dict) -> JournalEntryCreate:
    gross, provider, ref = int(d["gross"]), d.get("provider", "paystack"), d["payment_ref"]
    lines: list[JournalLineIn] = []
    _add(lines, "1020", dr=gross, sl=GATEWAY, sid=provider, desc=f"Customer payment {ref}")
    allocated = 0
    for a in d.get("allocations", []):
        allocated += int(a["gross"])
        _add(lines, "2010", cr=a["gross"], sl=VENDOR, sid=a["seller_id"],
             desc=f"Escrow for {a.get('seller_name', '')} - order {d['order_ref']}")
    fee = int(d.get("delivery_fee", 0))
    _add(lines, "2020", cr=fee, desc=f"Delivery fee held - order {d['order_ref']}")
    rest = gross - allocated - fee
    if rest < 0:
        raise ValueError("allocations plus delivery fee exceed the payment")
    _add(lines, "2010", cr=rest, desc=f"Unallocated customer deposit {ref} - needs review or refund")
    return _entry(key, TransactionType.ORDER_PAYMENT, ref, f"Payment captured {ref} ({d.get('channel', '')})", lines)


def _order_delivered(key: str, d: dict) -> JournalEntryCreate:
    gross, net, commission = int(d["gross"]), int(d["net"]), int(d["commission"])
    if gross != net + commission:
        raise ValueError("gross must equal net + commission")
    sid, name = d["seller_id"], d.get("seller_name", "")
    lines: list[JournalLineIn] = []
    _add(lines, "2010", dr=gross, sl=VENDOR, sid=sid, desc=f"Escrow released - order {d['order_ref']}")
    _add(lines, "2030", cr=net, sl=VENDOR, sid=sid, desc=f"Payable to {name} - order {d['order_ref']}")
    _add(lines, "4010", cr=commission, sl=VENDOR, sid=sid, desc=f"Commission on {name} - order {d['order_ref']}")
    return _entry(key, TransactionType.ESCROW_RELEASE, d["order_ref"], f"Delivered: escrow released for order {d['order_ref']}", lines)


def _delivery_fee_earned(key: str, d: dict) -> JournalEntryCreate:
    amount, ref = int(d["amount"]), d["order_ref"]
    lines: list[JournalLineIn] = []
    _add(lines, "2020", dr=amount, desc=f"Delivery fee released - order {ref}")
    _add(lines, "4020", cr=amount, desc=f"Delivery fee revenue - order {ref}")
    return _entry(key, TransactionType.ESCROW_RELEASE, ref, f"Delivery fee earned for order {ref}", lines)


def _rider_earning(key: str, d: dict) -> JournalEntryCreate:
    amount = int(d["amount"])
    lines: list[JournalLineIn] = []
    _add(lines, "5020", dr=amount, desc=f"Rider pay {d['source']}")
    _add(lines, "2040", cr=amount, sl=RIDER, sid=d["rider_id"], desc=f"Owed to {d.get('rider_name', '')} ({d['source']})")
    return _entry(key, TransactionType.RIDER_EARNING, d["source"], f"Rider earning accrued ({d['source']})", lines)


def _seller_payout_sent(key: str, d: dict) -> JournalEntryCreate:
    amount, fee, provider = int(d["amount"]), int(d.get("fee", 0)), d.get("provider", "paystack")
    lines: list[JournalLineIn] = []
    _add(lines, "2030", dr=amount, sl=VENDOR, sid=d["seller_id"], desc=f"Payout to {d.get('seller_name', '')}")
    _add(lines, "1020", cr=amount, sl=GATEWAY, sid=provider, desc=f"Transfer {d['payout_ref']}")
    _add(lines, "5010", dr=fee, desc=f"Transfer fee {d['payout_ref']}")
    _add(lines, "1020", cr=fee, sl=GATEWAY, sid=provider, desc=f"Transfer fee {d['payout_ref']}")
    return _entry(key, TransactionType.VENDOR_PAYOUT, d["payout_ref"], f"Seller payout sent {d['payout_ref']}", lines)


def _seller_payout_reversed(key: str, d: dict) -> JournalEntryCreate:
    amount, provider = int(d["amount"]), d.get("provider", "paystack")
    lines: list[JournalLineIn] = []
    _add(lines, "1020", dr=amount, sl=GATEWAY, sid=provider, desc=f"Transfer returned {d['payout_ref']}")
    _add(lines, "2030", cr=amount, sl=VENDOR, sid=d["seller_id"], desc=f"Payout returned to {d.get('seller_name', '')}")
    return _entry(key, TransactionType.VENDOR_PAYOUT, d["payout_ref"], f"Seller payout reversed {d['payout_ref']}", lines)


def _rider_payout_sent(key: str, d: dict) -> JournalEntryCreate:
    amount = int(d["amount"])
    lines: list[JournalLineIn] = []
    _add(lines, "2040", dr=amount, sl=RIDER, sid=d["rider_id"], desc=f"Payout to {d.get('rider_name', '')}")
    _add(lines, "1020", cr=amount, sl=GATEWAY, sid="paystack", desc=f"Transfer {d['payout_ref']}")
    return _entry(key, TransactionType.RIDER_PAYOUT, d["payout_ref"], f"Rider payout sent {d['payout_ref']}", lines)


def _refund_processed(key: str, d: dict) -> JournalEntryCreate:
    amount, provider = int(d["amount"]), d.get("provider", "paystack")
    lines: list[JournalLineIn] = []
    _add(lines, "1020", cr=amount, sl=GATEWAY, sid=provider, desc=f"Refund {d['refund_ref']}")
    for p in d.get("parts", []):
        sid, name = p["seller_id"], p.get("seller_name", "")
        if p["delivered"]:
            _add(lines, "2030", dr=p["seller_part"], sl=VENDOR, sid=sid, desc=f"Refund taken back from {name}")
            _add(lines, "4090", dr=p["commission_part"], sl=VENDOR, sid=sid, desc=f"Commission reversed on refund ({name})")
        else:
            _add(lines, "2010", dr=p["gross_part"], sl=VENDOR, sid=sid, desc=f"Escrow returned to customer ({name})")
    target = d.get("other_target", "2010")
    if target not in ("2010", "2020", "4090"):
        raise ValueError(f"unsupported other_target {target}")
    _add(lines, target, dr=d.get("other_part", 0), desc=f"Delivery fee / unallocated part of refund {d['refund_ref']}")
    return _entry(key, TransactionType.REFUND, d["refund_ref"], f"Customer refund {d['refund_ref']} on payment {d['payment_ref']}", lines)


_HANDLERS: dict[str, Callable[[str, dict], JournalEntryCreate]] = {
    "payment.captured": _payment_captured,
    "order.delivered": _order_delivered,
    "delivery_fee.earned": _delivery_fee_earned,
    "rider.earning_accrued": _rider_earning,
    "seller.payout_sent": _seller_payout_sent,
    "seller.payout_reversed": _seller_payout_reversed,
    "rider.payout_sent": _rider_payout_sent,
    "refund.processed": _refund_processed,
}


# ---------------------------------------------------------------------------
# names (so the vendor screens show "Mama Put Kitchen" instead of a UUID)
# ---------------------------------------------------------------------------

def _parties_in(d: dict) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}

    def add(kind: str, ident: Any, name: Any) -> None:
        if ident and name:
            found[(kind, str(ident))] = str(name)[:200]

    add("seller", d.get("seller_id"), d.get("seller_name"))
    add("rider", d.get("rider_id"), d.get("rider_name"))
    for item in [*d.get("allocations", []), *d.get("parts", [])]:
        add("seller", item.get("seller_id"), item.get("seller_name"))
    return found


async def _upsert_parties(db: AsyncSession, d: dict) -> None:
    for (kind, ident), name in _parties_in(d).items():
        stmt = pg_insert(Party).values(party_type=kind, external_id=ident, name=name)
        stmt = stmt.on_conflict_do_update(index_elements=["party_type", "external_id"], set_={"name": stmt.excluded.name})
        await db.execute(stmt)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def ingest_event(db: AsyncSession, event_key: str, event_type: str, data: dict) -> dict:
    handler = _HANDLERS.get(event_type)
    if handler is None:
        # Unknown type: acknowledge so the sender does not retry it forever.
        return {"status": "ignored", "reason": f"unknown event type '{event_type}'"}
    if not event_key:
        raise HTTPException(422, "event_key is required")
    try:
        entry_in = handler(event_key, data)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"Bad '{event_type}' event: {exc!r}")
    await _upsert_parties(db, data)
    entry = await service.post_journal_entry(db, entry_in)  # returns the existing entry if event_key was seen before
    await db.commit()
    return {"status": "posted", "entry_number": entry.entry_number}
