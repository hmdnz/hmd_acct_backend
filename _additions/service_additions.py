

# ---------------------------------------------------------------------------
# Added by cleanup_accounting.py: vendor position computed from the ledger alone
# ---------------------------------------------------------------------------
from app.modules.accounting.models import Party  # noqa: E402


async def get_vendor_subledger_summary(db: AsyncSession, vendor_id: uuid.UUID) -> VendorSubLedgerSummary:
    party = (await db.execute(
        select(Party).where(Party.party_type == "seller", Party.external_id == str(vendor_id))
    )).scalar_one_or_none()

    rows = (await db.execute(
        select(
            Account.code, JournalEntry.transaction_type,
            func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .select_from(JournalLine)
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(
            JournalLine.sub_ledger_type == SubLedgerType.VENDOR,
            JournalLine.sub_ledger_id == str(vendor_id),
            JournalEntry.status == JournalEntryStatus.POSTED,
        )
        .group_by(Account.code, JournalEntry.transaction_type)
    )).all()

    zero = Decimal("0")
    gross_sales = commissions = refunds = payouts = payable = zero
    for code, ttype, debit, credit in rows:
        debit, credit = Decimal(str(debit)), Decimal(str(credit))
        if code == "2010":
            gross_sales += credit                       # everything customers paid for this seller's goods
            if ttype == TransactionType.REFUND:
                refunds += debit                        # refunded before delivery
        elif code == "4010":
            commissions += credit                       # Hamads' commission, recognised at delivery
        elif code == "2030":
            payable += credit - debit                   # what Hamads owes the seller right now
            if ttype == TransactionType.VENDOR_PAYOUT:
                payouts += debit - credit               # money already sent (net of bounced transfers)
            elif ttype == TransactionType.REFUND:
                refunds += debit                        # refunded after delivery

    return VendorSubLedgerSummary(
        vendor_id=vendor_id,
        vendor_name=party.name if party else "Unknown vendor",
        gross_sales=_quantize(gross_sales),
        platform_commissions=_quantize(commissions),
        shipping_earned=_quantize(zero),
        withholding_tax=_quantize(zero),
        refunds_deducted=_quantize(refunds),
        reserves_held=_quantize(zero),
        total_payouts_completed=_quantize(payouts),
        pending_payouts=_quantize(zero),
        current_withdrawable_balance=_quantize(payable),
    )
