import csv
import io
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.accounting.models import (
    Account,
    AccountingSettings,
    AccountType,
    BankStatementRow,
    ExpenseBill,
    ExpenseBillStatus,
    FiscalPeriod,
    FiscalPeriodStatus,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    NormalBalance,
    PaymentTransaction,
    PaymentTransactionStatus,
    ReconciliationStatus,
    RefundRecord,
    RefundStatus,
    SubLedgerType,
    TaxRate,
    TransactionType,
    VendorPayout,
    VendorPayoutStatus,
    VendorReserveHold,
)
from app.modules.accounting.schemas import (
    AccountingDashboardKPIs,
    BalanceSheetReport,
    CashFlowReport,
    EscrowReleaseEvent,
    ExpenseBillCreate,
    GatewaySettlementEvent,
    JournalEntryCreate,
    OrderPaymentEvent,
    ProfitAndLossReport,
    RefundEvent,
    ReportLineItem,
    TrialBalanceItem,
    TrialBalanceReport,
    VendorPayoutCreate,
    VendorSubLedgerSummary,
)
from app.modules.auth.models import Seller


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _quantize(val: Decimal | float | int | str) -> Decimal:
    """Format decimal to 4 decimal places for accounting precision."""
    d = Decimal(str(val))
    return d.quantize(Decimal("0.0001"))


# ============================================================================
# 1. CORE DOUBLE-ENTRY ENGINE & IMMUTABILITY ENFORCEMENT
# ============================================================================

async def get_account_by_code(db: AsyncSession, code: str) -> Account:
    stmt = select(Account).where(Account.code == code)
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chart of Accounts code '{code}' not found. Please verify setup.",
        )
    if not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Account '{code}' ({account.name}) is inactive and cannot receive postings.",
        )
    return account


async def ensure_period_is_open(db: AsyncSession, posting_date: datetime) -> None:
    """Enforces fiscal period lockouts. Blocks posting into LOCKED or CLOSED periods."""
    stmt = select(FiscalPeriod).where(
        FiscalPeriod.start_date <= posting_date,
        FiscalPeriod.end_date >= posting_date,
    )
    period = (await db.execute(stmt)).scalar_one_or_none()
    if period and period.status in (FiscalPeriodStatus.LOCKED, FiscalPeriodStatus.CLOSED):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot post entry: Fiscal period '{period.name}' is {period.status.value}.",
        )


async def post_journal_entry(
    db: AsyncSession,
    entry_in: JournalEntryCreate,
    created_by: uuid.UUID | None = None,
    approved_by: uuid.UUID | None = None,
    reverses_entry_id: uuid.UUID | None = None,
) -> JournalEntry:
    """
    Creates and commits a strictly balanced double-entry journal posting.
    Enforces:
      1. Minimum 2 lines.
      2. Sum of Debits == Sum of Credits (within 0.0001 precision).
      3. Idempotency key deduplication.
      4. Period close checks.
      5. Immutability: Once saved with status=POSTED, cannot be modified.
    """
    if len(entry_in.lines) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Double-entry journal requires at least two lines (one debit, one credit).",
        )

    # 1. Idempotency Check
    if entry_in.idempotency_key:
        existing_stmt = select(JournalEntry).where(JournalEntry.idempotency_key == entry_in.idempotency_key)
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            return existing

    # 2. Fiscal Period Check
    now = _utc_now()
    await ensure_period_is_open(db, now)

    # 3. Resolve accounts and calculate balance sums
    total_debit = Decimal("0.0000")
    total_credit = Decimal("0.0000")
    line_models: list[JournalLine] = []

    for idx, line in enumerate(entry_in.lines):
        debit = _quantize(line.debit)
        credit = _quantize(line.credit)

        if debit > 0 and credit > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx+1}: A single line cannot have both a debit and credit balance.",
            )
        if debit == 0 and credit == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx+1}: Line must specify either a debit or credit greater than 0.",
            )

        account = await get_account_by_code(db, line.account_code)
        total_debit += debit
        total_credit += credit

        fx_rate = line.fx_rate or Decimal("1.000000")
        base_debit = _quantize(debit * fx_rate)
        base_credit = _quantize(credit * fx_rate)

        line_model = JournalLine(
            account_id=account.id,
            sub_ledger_type=line.sub_ledger_type,
            sub_ledger_id=line.sub_ledger_id,
            debit=debit,
            credit=credit,
            currency=line.currency or entry_in.currency,
            fx_rate=fx_rate,
            base_debit=base_debit,
            base_credit=base_credit,
            description=line.description or entry_in.narration,
        )
        line_models.append(line_model)

    # 4. Strict double-entry balance check: Debits == Credits
    imbalance = abs(total_debit - total_credit)
    if imbalance >= Decimal("0.0001"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Double-entry violation: Total debits ({total_debit}) do not equal "
                f"total credits ({total_credit}). Imbalance: {imbalance}"
            ),
        )

    # 5. Generate sequential entry number
    date_prefix = now.strftime("%Y%m%d")
    count_stmt = select(func.count(JournalEntry.id)).where(
        JournalEntry.entry_number.like(f"JE-{date_prefix}-%")
    )
    today_count = (await db.execute(count_stmt)).scalar_one()
    entry_number = f"JE-{date_prefix}-{today_count + 1:05d}"

    # 6. Construct Journal Entry Header
    entry = JournalEntry(
        entry_number=entry_number,
        idempotency_key=entry_in.idempotency_key,
        transaction_type=entry_in.transaction_type,
        reference_id=entry_in.reference_id,
        narration=entry_in.narration,
        status=JournalEntryStatus.POSTED,
        currency=entry_in.currency,
        posted_at=now,
        created_by=created_by,
        approved_by=approved_by,
        reverses_entry_id=reverses_entry_id,
        lines=line_models,
    )

    db.add(entry)
    await db.flush()
    return entry


async def reverse_journal_entry(
    db: AsyncSession,
    entry_id: uuid.UUID,
    reason: str,
    admin_id: uuid.UUID | None = None,
) -> JournalEntry:
    """
    IFRS Immutability enforcement:
    Existing journal entries cannot be deleted or mutated. To negate an entry,
    an exact offsetting entry is generated and linked.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry_id)
    )
    original = (await db.execute(stmt)).scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Journal entry not found")

    if original.status == JournalEntryStatus.VOIDED or original.reversed_by_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Journal entry has already been reversed.",
        )

    from app.modules.accounting.schemas import JournalLineIn

    reversal_lines: list[JournalLineIn] = []
    for line in original.lines:
        reversal_lines.append(
            JournalLineIn(
                account_code=line.account.code,
                debit=line.credit,  # Inverted
                credit=line.debit,  # Inverted
                sub_ledger_type=line.sub_ledger_type,
                sub_ledger_id=line.sub_ledger_id,
                currency=line.currency,
                fx_rate=line.fx_rate,
                description=f"REVERSAL of {original.entry_number}: {line.description}",
            )
        )

    reversal_payload = JournalEntryCreate(
        transaction_type=TransactionType.ADJUSTMENT,
        reference_id=f"REV-{original.entry_number}",
        narration=f"Reversal of {original.entry_number}: {reason}",
        currency=original.currency,
        lines=reversal_lines,
    )

    reversal_entry = await post_journal_entry(
        db,
        entry_in=reversal_payload,
        created_by=admin_id,
        approved_by=admin_id,
        reverses_entry_id=original.id,
    )

    original.reversed_by_id = reversal_entry.id
    original.status = JournalEntryStatus.VOIDED
    await db.commit()
    await db.refresh(reversal_entry)
    return reversal_entry


# ============================================================================
# 2. EVENT-DRIVEN ORDER LIFECYCLE & IFRS 15 REVENUE RECOGNITION
# ============================================================================

async def post_order_payment(db: AsyncSession, payload: OrderPaymentEvent) -> JournalEntry:
    """
    Lifecycle Event 1: Customer Order Placed & Paid.
    Accounting Treatment:
      Dr. 1020 Gateway Clearing Account (Asset increases with pending settlement)
      Cr. 2010 Customer Escrow Liability (Platform obligation to fulfill)
      Cr. 2020 Customer Shipping Fee Liability (Shipping collected in escrow)
    """
    total = _quantize(payload.total_amount)
    shipping = _quantize(payload.shipping_fee)
    escrow_merchandise = _quantize(total - shipping)

    # 1. Record Payment Transaction
    idempotency_key = payload.idempotency_key or f"PAY-{payload.gateway_reference}"
    tx_stmt = select(PaymentTransaction).where(PaymentTransaction.provider_reference == payload.gateway_reference)
    existing_tx = (await db.execute(tx_stmt)).scalar_one_or_none()

    from app.modules.accounting.schemas import JournalLineIn

    lines = [
        JournalLineIn(
            account_code="1020",
            debit=total,
            credit=Decimal("0.0000"),
            sub_ledger_type=SubLedgerType.GATEWAY,
            sub_ledger_id=payload.gateway_reference,
            currency=payload.currency,
            description=f"Gateway funds pending settlement - Ref: {payload.gateway_reference}",
        ),
        JournalLineIn(
            account_code="2010",
            debit=Decimal("0.0000"),
            credit=escrow_merchandise,
            sub_ledger_type=SubLedgerType.CUSTOMER,
            sub_ledger_id=str(payload.customer_id),
            currency=payload.currency,
            description=f"Customer escrow hold for Order {payload.order_id}",
        ),
    ]

    if shipping > Decimal("0.0000"):
        lines.append(
            JournalLineIn(
                account_code="2020",
                debit=Decimal("0.0000"),
                credit=shipping,
                sub_ledger_type=SubLedgerType.CUSTOMER,
                sub_ledger_id=str(payload.customer_id),
                currency=payload.currency,
                description=f"Customer shipping deposit for Order {payload.order_id}",
            )
        )

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.ORDER_PAYMENT,
            reference_id=str(payload.order_id),
            idempotency_key=idempotency_key,
            narration=f"Payment received for Order {payload.order_id} via {payload.provider} ref {payload.gateway_reference}",
            currency=payload.currency,
            lines=lines,
        ),
    )

    if not existing_tx:
        payment_record = PaymentTransaction(
            provider=payload.provider,
            provider_reference=payload.gateway_reference,
            idempotency_key=idempotency_key,
            order_id=payload.order_id,
            customer_id=payload.customer_id,
            currency=payload.currency,
            gross_amount=total,
            provider_fee=Decimal("0.0000"),
            net_amount=total,
            status=PaymentTransactionStatus.CAPTURED,
            journal_entry_id=entry.id,
        )
        db.add(payment_record)

    await db.commit()
    return entry


async def post_escrow_release(db: AsyncSession, payload: EscrowReleaseEvent) -> JournalEntry:
    """
    Lifecycle Event 3: Order Delivered & Escrow Released (IFRS 15 Revenue Recognition Point).
    Platform acts as Agent:
      - Escrow liability is debited (obligation fulfilled).
      - Platform Commission recognized in Revenue (4010).
      - Fulfillment Revenue recognized (4020).
      - Dispute safety reserve hold credited (1040).
      - Withholding tax credited (2055).
      - Net earnings credited to Merchant Payable (2030) for vendor payout.
    """
    idempotency_key = f"ESCROW-REL-{payload.seller_order_id}"
    subtotal = _quantize(payload.subtotal)
    shipping = _quantize(payload.shipping_fee_earned)
    commission = _quantize(subtotal * payload.commission_rate)
    reserve_hold = _quantize(subtotal * payload.reserve_hold_rate)
    wht = _quantize((subtotal - commission) * payload.withholding_tax_rate)
    net_merchant_payable = _quantize(subtotal - commission - reserve_hold - wht)

    from app.modules.accounting.schemas import JournalLineIn

    lines = [
        # Release merchandise escrow
        JournalLineIn(
            account_code="2010",
            debit=subtotal,
            credit=Decimal("0.0000"),
            sub_ledger_type=SubLedgerType.VENDOR,
            sub_ledger_id=str(payload.seller_id),
            description=f"Escrow release for delivered SellerOrder {payload.seller_order_id}",
        ),
    ]

    if shipping > Decimal("0.0000"):
        lines.append(
            JournalLineIn(
                account_code="2020",
                debit=shipping,
                credit=Decimal("0.0000"),
                sub_ledger_type=SubLedgerType.NONE,
                description=f"Release shipping liability to earned fulfillment fee",
            )
        )
        lines.append(
            JournalLineIn(
                account_code="4020",
                debit=Decimal("0.0000"),
                credit=shipping,
                sub_ledger_type=SubLedgerType.NONE,
                description=f"Platform fulfillment revenue earned on Order {payload.order_id}",
            )
        )

    # Credit Net Merchant Payable
    lines.append(
        JournalLineIn(
            account_code="2030",
            debit=Decimal("0.0000"),
            credit=net_merchant_payable,
            sub_ledger_type=SubLedgerType.VENDOR,
            sub_ledger_id=str(payload.seller_id),
            description=f"Net earnings credited to vendor payable - Order {payload.order_id}",
        )
    )

    # Credit Platform Commission (IFRS 15 Agency Revenue)
    lines.append(
        JournalLineIn(
            account_code="4010",
            debit=Decimal("0.0000"),
            credit=commission,
            sub_ledger_type=SubLedgerType.VENDOR,
            sub_ledger_id=str(payload.seller_id),
            description=f"Platform commission ({payload.commission_rate*100:.1f}%) on Order {payload.order_id}",
        )
    )

    # Credit Reserve Hold
    if reserve_hold > Decimal("0.0000"):
        lines.append(
            JournalLineIn(
                account_code="1040",
                debit=Decimal("0.0000"),
                credit=reserve_hold,
                sub_ledger_type=SubLedgerType.VENDOR,
                sub_ledger_id=str(payload.seller_id),
                description=f"Dispute reserve hold ({payload.reserve_hold_rate*100:.1f}%) on SellerOrder {payload.seller_order_id}",
            )
        )
        from datetime import timedelta
        release_date = _utc_now() + timedelta(days=payload.reserve_days)
        reserve_record = VendorReserveHold(
            vendor_id=payload.seller_id,
            reference_id=str(payload.seller_order_id),
            hold_amount=reserve_hold,
            status="HELD",
            release_due_date=release_date,
        )
        db.add(reserve_record)

    # Credit Withholding Tax
    if wht > Decimal("0.0000"):
        lines.append(
            JournalLineIn(
                account_code="2055",
                debit=Decimal("0.0000"),
                credit=wht,
                sub_ledger_type=SubLedgerType.VENDOR,
                sub_ledger_id=str(payload.seller_id),
                description=f"Withholding tax deducted on Order {payload.order_id}",
            )
        )

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.ESCROW_RELEASE,
            reference_id=str(payload.seller_order_id),
            idempotency_key=idempotency_key,
            narration=f"IFRS 15 Escrow release & commission recognition for SellerOrder {payload.seller_order_id}",
            lines=lines,
        ),
    )
    await db.commit()
    return entry


async def process_seller_order_delivery(
    db: AsyncSession, seller_order_id: uuid.UUID, commission_rate: Decimal = Decimal("0.1000")
) -> JournalEntry:
    """Convenience trigger to release escrow when a SellerOrder is confirmed delivered."""
    from app.modules.orders.models import SellerOrder

    so = await db.get(SellerOrder, seller_order_id)
    if not so:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Seller order not found")

    payload = EscrowReleaseEvent(
        seller_order_id=so.id,
        order_id=so.order_id,
        seller_id=so.seller_id,
        subtotal=so.subtotal,
        commission_rate=commission_rate,
        shipping_fee_earned=Decimal("0.0000"),
        withholding_tax_rate=Decimal("0.0500"),
        reserve_hold_rate=Decimal("0.0500"),
        reserve_days=14,
    )
    return await post_escrow_release(db, payload)


async def post_order_refund(db: AsyncSession, payload: RefundEvent) -> JournalEntry:
    """
    Refunds & Disputes Handling:
      - If refund occurs prior to fulfillment: reverses customer escrow against gateway/bank clearing.
      - If refund occurs post-delivery: reverses vendor payable and absorbs commission based on dispute policy.
    """
    amount = _quantize(payload.refund_amount)
    from app.modules.accounting.schemas import JournalLineIn

    if payload.seller_id:
        lines = [
            JournalLineIn(
                account_code="2030",
                debit=amount,
                credit=Decimal("0.0000"),
                sub_ledger_type=SubLedgerType.VENDOR,
                sub_ledger_id=str(payload.seller_id),
                description=f"Refund clawback from vendor: {payload.reason}",
            ),
            JournalLineIn(
                account_code="1020",
                debit=Decimal("0.0000"),
                credit=amount,
                sub_ledger_type=SubLedgerType.CUSTOMER,
                sub_ledger_id=str(payload.customer_id),
                description=f"Gateway refund payout to customer - Ref Order {payload.order_id}",
            ),
        ]
    else:
        lines = [
            JournalLineIn(
                account_code="2010",
                debit=amount,
                credit=Decimal("0.0000"),
                sub_ledger_type=SubLedgerType.CUSTOMER,
                sub_ledger_id=str(payload.customer_id),
                description=f"Customer escrow cancellation refund: {payload.reason}",
            ),
            JournalLineIn(
                account_code="1020",
                debit=Decimal("0.0000"),
                credit=amount,
                sub_ledger_type=SubLedgerType.CUSTOMER,
                sub_ledger_id=str(payload.customer_id),
                description=f"Customer refund payout via gateway - Order {payload.order_id}",
            ),
        ]

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.REFUND,
            reference_id=str(payload.order_id),
            narration=f"Refund processed for Order {payload.order_id}: {payload.reason}",
            lines=lines,
        ),
    )

    # Record Refund tracking record
    refund_rec = RefundRecord(
        order_id=payload.order_id,
        seller_order_id=payload.seller_order_id,
        customer_id=payload.customer_id,
        amount=amount,
        reason=payload.reason,
        status=RefundStatus.COMPLETED,
        journal_entry_id=entry.id,
    )
    db.add(refund_rec)
    await db.commit()
    return entry


async def post_gateway_settlement(db: AsyncSession, payload: GatewaySettlementEvent) -> JournalEntry:
    """
    Payment Gateway Bank Settlement:
      Clears in-transit gateway funds into the operational bank account.
      Dr. 1010 Operating Bank Account (Net received in bank)
      Dr. 5010 Payment Gateway Processing Fees (Transaction expense)
      Cr. 1020 Payment Gateway Clearing Account (Gross settlement cleared)
    """
    gross = _quantize(payload.gross_settlement)
    fee = _quantize(payload.gateway_fee)
    net_bank = _quantize(payload.net_bank_amount)

    if abs((net_bank + fee) - gross) >= Decimal("0.0001"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settlement mismatch: Net ({net_bank}) + Fee ({fee}) != Gross ({gross})",
        )

    from app.modules.accounting.schemas import JournalLineIn

    lines = [
        JournalLineIn(
            account_code="1010",
            debit=net_bank,
            credit=Decimal("0.0000"),
            sub_ledger_type=SubLedgerType.GATEWAY,
            sub_ledger_id=payload.gateway_reference,
            description=f"Net settlement deposited to Bank from {payload.gateway_name}",
        ),
        JournalLineIn(
            account_code="1020",
            debit=Decimal("0.0000"),
            credit=gross,
            sub_ledger_type=SubLedgerType.GATEWAY,
            sub_ledger_id=payload.gateway_reference,
            description=f"Gateway clearing account cleared for batch {payload.gateway_reference}",
        ),
    ]

    if fee > Decimal("0.0000"):
        lines.append(
            JournalLineIn(
                account_code="5010",
                debit=fee,
                credit=Decimal("0.0000"),
                sub_ledger_type=SubLedgerType.GATEWAY,
                sub_ledger_id=payload.gateway_reference,
                description=f"{payload.gateway_name} merchant processing charge",
            )
        )

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.GATEWAY_SETTLEMENT,
            reference_id=payload.gateway_reference,
            narration=f"Settlement reconciliation for {payload.gateway_name} ref {payload.gateway_reference}",
            lines=lines,
        ),
    )
    await db.commit()
    return entry


# ============================================================================
# 3. VENDOR SUB-LEDGER & PAYOUTS WITH CONCURRENCY ROW LOCKING
# ============================================================================

async def get_vendor_withdrawable_balance(db: AsyncSession, vendor_id: uuid.UUID) -> Decimal:
    """
    Calculates real-time withdrawable balance from Merchant Payables (2030)
    for this vendor, subtracting any PENDING or PROCESSING payouts.
    """
    account_stmt = select(Account.id).where(Account.code == "2030")
    account_id = (await db.execute(account_stmt)).scalar_one_or_none()
    if not account_id:
        return Decimal("0.0000")

    lines_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit), 0).label("credits"),
            func.coalesce(func.sum(JournalLine.debit), 0).label("debits"),
        )
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == account_id,
            JournalLine.sub_ledger_type == SubLedgerType.VENDOR,
            JournalLine.sub_ledger_id == str(vendor_id),
            JournalEntry.status == JournalEntryStatus.POSTED,
        )
    )
    res = (await db.execute(lines_stmt)).one()
    ledger_balance = Decimal(str(res.credits)) - Decimal(str(res.debits))

    pending_payouts_stmt = select(
        func.coalesce(func.sum(VendorPayout.amount), 0)
    ).where(
        VendorPayout.vendor_id == vendor_id,
        VendorPayout.status.in_([VendorPayoutStatus.PENDING, VendorPayoutStatus.PROCESSING, VendorPayoutStatus.APPROVED]),
    )
    pending_amount = (await db.execute(pending_payouts_stmt)).scalar_one()

    return _quantize(ledger_balance - Decimal(str(pending_amount)))


async def get_vendor_subledger_summary(db: AsyncSession, vendor_id: uuid.UUID) -> VendorSubLedgerSummary:
    """Detailed vendor financial position breakdown."""
    seller = await db.get(Seller, vendor_id)
    vendor_name = f"{seller.brand_name} ({seller.first_name} {seller.last_name})" if seller else "Unknown Vendor"

    stmt = (
        select(Account.code, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        .join(JournalLine, Account.id == JournalLine.account_id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(
            JournalLine.sub_ledger_type == SubLedgerType.VENDOR,
            JournalLine.sub_ledger_id == str(vendor_id),
            JournalEntry.status == JournalEntryStatus.POSTED,
        )
        .group_by(Account.code)
    )
    rows = (await db.execute(stmt)).all()

    gross_sales = Decimal("0.0000")
    commissions = Decimal("0.0000")
    shipping_earned = Decimal("0.0000")
    wht = Decimal("0.0000")
    refunds = Decimal("0.0000")
    reserves_held = Decimal("0.0000")

    for code, d, c in rows:
        d = Decimal(str(d or 0))
        c = Decimal(str(c or 0))
        if code == "2010":
            gross_sales += d
        elif code == "4010":
            commissions += c
        elif code == "4020":
            shipping_earned += c
        elif code == "2055":
            wht += c
        elif code == "1040":
            reserves_held += c - d

    completed_stmt = select(func.coalesce(func.sum(VendorPayout.amount), 0)).where(
        VendorPayout.vendor_id == vendor_id,
        VendorPayout.status == VendorPayoutStatus.COMPLETED,
    )
    completed_payouts = Decimal(str((await db.execute(completed_stmt)).scalar_one()))

    pending_stmt = select(func.coalesce(func.sum(VendorPayout.amount), 0)).where(
        VendorPayout.vendor_id == vendor_id,
        VendorPayout.status.in_([VendorPayoutStatus.PENDING, VendorPayoutStatus.PROCESSING, VendorPayoutStatus.APPROVED]),
    )
    pending_payouts = Decimal(str((await db.execute(pending_stmt)).scalar_one()))

    withdrawable = await get_vendor_withdrawable_balance(db, vendor_id)

    return VendorSubLedgerSummary(
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        gross_sales=_quantize(gross_sales),
        platform_commissions=_quantize(commissions),
        shipping_earned=_quantize(shipping_earned),
        withholding_tax=_quantize(wht),
        refunds_deducted=_quantize(refunds),
        reserves_held=_quantize(reserves_held),
        total_payouts_completed=_quantize(completed_payouts),
        pending_payouts=_quantize(pending_payouts),
        current_withdrawable_balance=_quantize(withdrawable),
    )


async def create_vendor_payout(
    db: AsyncSession, payload: VendorPayoutCreate, admin_id: uuid.UUID | None = None
) -> VendorPayout:
    """
    Requests a vendor payout with strict row-level locking on the seller record
    to prevent race conditions / double withdrawals.
    """
    amount = _quantize(payload.amount)

    seller_stmt = select(Seller).where(Seller.id == payload.vendor_id).with_for_update()
    seller = (await db.execute(seller_stmt)).scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    if not seller.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vendor account is deactivated")

    available = await get_vendor_withdrawable_balance(db, payload.vendor_id)
    if amount > available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient withdrawable funds. Requested: {amount}, Available: {available}",
        )

    now = _utc_now()
    payout_ref = f"PO-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    payout = VendorPayout(
        payout_reference=payout_ref,
        vendor_id=payload.vendor_id,
        amount=amount,
        fee_deducted=Decimal("0.0000"),
        net_amount=amount,
        bank_account_details=json.dumps(payload.bank_account_details),
        status=VendorPayoutStatus.PENDING,
        approved_by=admin_id,
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)
    return payout


async def complete_vendor_payout(
    db: AsyncSession, payout_id: uuid.UUID, gateway_reference: str
) -> VendorPayout:
    """
    Completes vendor payout post-bank confirmation:
      Dr. 2030 Merchant Payables (Liability satisfied)
      Cr. 1010 Operating Bank Account (Cash outflow)
    """
    stmt = select(VendorPayout).where(VendorPayout.id == payout_id).with_for_update()
    payout = (await db.execute(stmt)).scalar_one_or_none()
    if not payout:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payout record not found")

    if payout.status == VendorPayoutStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payout already completed")

    from app.modules.accounting.schemas import JournalLineIn

    amount = _quantize(payout.amount)
    lines = [
        JournalLineIn(
            account_code="2030",
            debit=amount,
            credit=Decimal("0.0000"),
            sub_ledger_type=SubLedgerType.VENDOR,
            sub_ledger_id=str(payout.vendor_id),
            description=f"Vendor payout completed - Ref: {payout.payout_reference}",
        ),
        JournalLineIn(
            account_code="1010",
            debit=Decimal("0.0000"),
            credit=amount,
            sub_ledger_type=SubLedgerType.VENDOR,
            sub_ledger_id=str(payout.vendor_id),
            description=f"Bank wire outflow for vendor payout {payout.payout_reference}",
        ),
    ]

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.VENDOR_PAYOUT,
            reference_id=payout.payout_reference,
            narration=f"Disbursement of vendor funds for payout {payout.payout_reference}",
            lines=lines,
        ),
    )

    payout.status = VendorPayoutStatus.COMPLETED
    payout.gateway_reference = gateway_reference
    payout.journal_entry_id = entry.id
    payout.processed_at = _utc_now()
    await db.commit()
    await db.refresh(payout)
    return payout


# ============================================================================
# 4. OPERATING EXPENSES
# ============================================================================

async def create_expense_bill(db: AsyncSession, payload: ExpenseBillCreate, admin_id: uuid.UUID | None = None) -> ExpenseBill:
    """Records an operating expense bill (e.g. AWS, logistics courier invoice)."""
    account = await get_account_by_code(db, payload.expense_account_code)
    subtotal = _quantize(payload.subtotal)
    tax = _quantize(payload.tax_amount)
    total = _quantize(subtotal + tax)

    bill = ExpenseBill(
        vendor_name=payload.vendor_name,
        expense_account_id=account.id,
        invoice_number=payload.invoice_number,
        subtotal=subtotal,
        tax_amount=tax,
        total_amount=total,
        currency=payload.currency,
        status=ExpenseBillStatus.APPROVED,
        due_date=payload.due_date,
        approved_by=admin_id,
    )
    db.add(bill)
    await db.commit()
    await db.refresh(bill)
    return bill


async def pay_expense_bill(db: AsyncSession, bill_id: uuid.UUID, payment_method_code: str = "1010") -> ExpenseBill:
    """Executes expense bill disbursement: Dr Expense Account, Cr Operating Bank."""
    bill = await db.get(ExpenseBill, bill_id)
    if not bill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense bill not found")
    if bill.status == ExpenseBillStatus.PAID:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bill already paid")

    account = await db.get(Account, bill.expense_account_id)
    from app.modules.accounting.schemas import JournalLineIn

    lines = [
        JournalLineIn(
            account_code=account.code,
            debit=bill.total_amount,
            credit=Decimal("0.0000"),
            sub_ledger_type=SubLedgerType.EXPENSE_VENDOR,
            sub_ledger_id=bill.vendor_name,
            description=f"Expense bill payment: {bill.vendor_name} inv {bill.invoice_number or 'N/A'}",
        ),
        JournalLineIn(
            account_code=payment_method_code,
            debit=Decimal("0.0000"),
            credit=bill.total_amount,
            sub_ledger_type=SubLedgerType.EXPENSE_VENDOR,
            sub_ledger_id=bill.vendor_name,
            description=f"Bank disbursement for {bill.vendor_name}",
        ),
    ]

    entry = await post_journal_entry(
        db,
        JournalEntryCreate(
            transaction_type=TransactionType.EXPENSE_PAYMENT,
            reference_id=str(bill.id),
            narration=f"Disbursement for operating bill: {bill.vendor_name}",
            lines=lines,
        ),
    )

    bill.status = ExpenseBillStatus.PAID
    bill.paid_at = _utc_now()
    bill.journal_entry_id = entry.id
    await db.commit()
    await db.refresh(bill)
    return bill


# ============================================================================
# 5. FINANCIAL REPORTING ENGINE (IFRS & IAS COMPLIANT)
# ============================================================================

async def generate_trial_balance(db: AsyncSession, as_of_date: datetime | None = None) -> TrialBalanceReport:
    """Verifies double-entry equilibrium across all accounts."""
    cutoff = as_of_date or _utc_now()

    stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0).label("total_debit"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("total_credit"),
        )
        .outerjoin(JournalLine, Account.id == JournalLine.account_id)
        .outerjoin(
            JournalEntry,
            (JournalLine.journal_entry_id == JournalEntry.id)
            & (JournalEntry.posted_at <= cutoff)
            & (JournalEntry.status == JournalEntryStatus.POSTED),
        )
        .group_by(Account.id, Account.code, Account.name, Account.type)
        .order_by(Account.code)
    )

    rows = (await db.execute(stmt)).all()

    items: list[TrialBalanceItem] = []
    grand_total_debits = Decimal("0.0000")
    grand_total_credits = Decimal("0.0000")

    for row in rows:
        dr = Decimal(str(row.total_debit))
        cr = Decimal(str(row.total_credit))

        if row.type in (AccountType.ASSET, AccountType.EXPENSE):
            net = dr - cr
            net_dr = _quantize(max(Decimal("0.0000"), net))
            net_cr = _quantize(max(Decimal("0.0000"), -net))
        else:
            net = cr - dr
            net_dr = _quantize(max(Decimal("0.0000"), -net))
            net_cr = _quantize(max(Decimal("0.0000"), net))

        grand_total_debits += net_dr
        grand_total_credits += net_cr

        items.append(
            TrialBalanceItem(
                account_id=row.id,
                code=row.code,
                name=row.name,
                account_type=row.type,
                debit_balance=net_dr,
                credit_balance=net_cr,
            )
        )

    imbalance = abs(grand_total_debits - grand_total_credits)
    return TrialBalanceReport(
        as_of_date=cutoff,
        items=items,
        total_debits=_quantize(grand_total_debits),
        total_credits=_quantize(grand_total_credits),
        is_balanced=imbalance < Decimal("0.0001"),
        imbalance=_quantize(imbalance),
    )


async def generate_profit_and_loss(
    db: AsyncSession, start_date: datetime | None = None, end_date: datetime | None = None
) -> ProfitAndLossReport:
    """Statement of Profit or Loss (Income Statement - IFRS 15 Agent Model)."""
    cutoff_end = end_date or _utc_now()

    stmt = (
        select(
            Account.code,
            Account.name,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0).label("debits"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("credits"),
        )
        .join(JournalLine, Account.id == JournalLine.account_id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(
            Account.type.in_([AccountType.REVENUE, AccountType.EXPENSE]),
            JournalEntry.posted_at <= cutoff_end,
            JournalEntry.status == JournalEntryStatus.POSTED,
        )
    )
    if start_date:
        stmt = stmt.where(JournalEntry.posted_at >= start_date)

    stmt = stmt.group_by(Account.code, Account.name, Account.type).order_by(Account.code)
    rows = (await db.execute(stmt)).all()

    revenues: list[ReportLineItem] = []
    cost_of_sales: list[ReportLineItem] = []
    operating_expenses: list[ReportLineItem] = []

    total_revenue = Decimal("0.0000")
    total_cost_of_sales = Decimal("0.0000")
    total_operating_expenses = Decimal("0.0000")

    for row in rows:
        dr = Decimal(str(row.debits))
        cr = Decimal(str(row.credits))

        if row.type == AccountType.REVENUE:
            net_rev = _quantize(cr - dr)
            if net_rev != 0:
                revenues.append(ReportLineItem(code=row.code, name=row.name, amount=net_rev))
                total_revenue += net_rev
        elif row.type == AccountType.EXPENSE:
            net_exp = _quantize(dr - cr)
            if net_exp != 0:
                if row.code in ("5010", "5020"):
                    cost_of_sales.append(ReportLineItem(code=row.code, name=row.name, amount=net_exp))
                    total_cost_of_sales += net_exp
                else:
                    operating_expenses.append(ReportLineItem(code=row.code, name=row.name, amount=net_exp))
                    total_operating_expenses += net_exp

    gross_profit = total_revenue - total_cost_of_sales
    net_profit = gross_profit - total_operating_expenses

    gross_margin_pct = (
        _quantize((gross_profit / total_revenue) * Decimal("100.0")) if total_revenue > 0 else Decimal("0.0")
    )
    net_margin_pct = (
        _quantize((net_profit / total_revenue) * Decimal("100.0")) if total_revenue > 0 else Decimal("0.0")
    )

    return ProfitAndLossReport(
        period_start=start_date,
        period_end=cutoff_end,
        operating_revenues=revenues,
        total_revenue=_quantize(total_revenue),
        cost_of_sales=cost_of_sales,
        total_cost_of_sales=_quantize(total_cost_of_sales),
        gross_profit=_quantize(gross_profit),
        gross_profit_margin_pct=gross_margin_pct,
        operating_expenses=operating_expenses,
        total_operating_expenses=_quantize(total_operating_expenses),
        net_profit=_quantize(net_profit),
        net_profit_margin_pct=net_margin_pct,
    )


async def generate_balance_sheet(db: AsyncSession, as_of_date: datetime | None = None) -> BalanceSheetReport:
    """Statement of Financial Position (Balance Sheet - IAS 1)."""
    cutoff = as_of_date or _utc_now()

    stmt = (
        select(
            Account.code,
            Account.name,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0).label("debits"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("credits"),
        )
        .join(JournalLine, Account.id == JournalLine.account_id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.posted_at <= cutoff, JournalEntry.status == JournalEntryStatus.POSTED)
        .group_by(Account.code, Account.name, Account.type)
        .order_by(Account.code)
    )
    rows = (await db.execute(stmt)).all()

    assets: list[ReportLineItem] = []
    liabilities: list[ReportLineItem] = []
    equity_items: list[ReportLineItem] = []

    total_assets = Decimal("0.0000")
    total_liabilities = Decimal("0.0000")
    total_equity_base = Decimal("0.0000")
    current_earnings = Decimal("0.0000")

    for row in rows:
        dr = Decimal(str(row.debits))
        cr = Decimal(str(row.credits))

        if row.type == AccountType.ASSET:
            net = _quantize(dr - cr)
            if net != 0:
                assets.append(ReportLineItem(code=row.code, name=row.name, amount=net))
                total_assets += net
        elif row.type == AccountType.LIABILITY:
            net = _quantize(cr - dr)
            if net != 0:
                liabilities.append(ReportLineItem(code=row.code, name=row.name, amount=net))
                total_liabilities += net
        elif row.type == AccountType.EQUITY:
            net = _quantize(cr - dr)
            if net != 0:
                equity_items.append(ReportLineItem(code=row.code, name=row.name, amount=net))
                total_equity_base += net
        elif row.type == AccountType.REVENUE:
            current_earnings += _quantize(cr - dr)
        elif row.type == AccountType.EXPENSE:
            current_earnings -= _quantize(dr - cr)

    total_equity = total_equity_base + current_earnings
    total_liab_and_equity = total_liabilities + total_equity
    imbalance = abs(total_assets - total_liab_and_equity)

    return BalanceSheetReport(
        as_of_date=cutoff,
        current_assets=assets,
        total_assets=_quantize(total_assets),
        current_liabilities=liabilities,
        total_liabilities=_quantize(total_liabilities),
        equity_items=equity_items,
        current_period_earnings=_quantize(current_earnings),
        total_equity=_quantize(total_equity),
        total_liabilities_and_equity=_quantize(total_liab_and_equity),
        is_balanced=imbalance < Decimal("0.0001"),
        imbalance=_quantize(imbalance),
    )


async def generate_cash_flow_statement(db: AsyncSession, as_of_date: datetime | None = None) -> CashFlowReport:
    """Statement of Cash Flows (IAS 7 Indirect Method)."""
    cutoff = as_of_date or _utc_now()
    pnl = await generate_profit_and_loss(db, end_date=cutoff)

    bank_lines = (
        await db.execute(
            select(
                func.coalesce(func.sum(JournalLine.debit), 0),
                func.coalesce(func.sum(JournalLine.credit), 0),
            )
            .join(Account, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .where(
                Account.code.in_(["1010", "1020"]),
                JournalEntry.posted_at <= cutoff,
                JournalEntry.status == JournalEntryStatus.POSTED,
            )
        )
    ).one()
    ending_cash = _quantize(Decimal(str(bank_lines[0])) - Decimal(str(bank_lines[1])))

    escrow_lines = (
        await db.execute(
            select(
                func.coalesce(func.sum(JournalLine.credit), 0),
                func.coalesce(func.sum(JournalLine.debit), 0),
            )
            .join(Account, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .where(
                Account.code.in_(["2010", "2020"]),
                JournalEntry.posted_at <= cutoff,
                JournalEntry.status == JournalEntryStatus.POSTED,
            )
        )
    ).one()
    escrow_change = _quantize(Decimal(str(escrow_lines[0])) - Decimal(str(escrow_lines[1])))

    payables_lines = (
        await db.execute(
            select(
                func.coalesce(func.sum(JournalLine.credit), 0),
                func.coalesce(func.sum(JournalLine.debit), 0),
            )
            .join(Account, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
            .where(
                Account.code == "2030",
                JournalEntry.posted_at <= cutoff,
                JournalEntry.status == JournalEntryStatus.POSTED,
            )
        )
    ).one()
    payables_change = _quantize(Decimal(str(payables_lines[0])) - Decimal(str(payables_lines[1])))

    operating_items = [
        ReportLineItem(code="NP", name="Net Operating Profit (Loss)", amount=pnl.net_profit),
        ReportLineItem(code="WC-01", name="Change in Customer Escrow Deposits", amount=escrow_change),
        ReportLineItem(code="WC-02", name="Change in Merchant Payables Held", amount=payables_change),
    ]

    net_cash_operating = pnl.net_profit + escrow_change + payables_change

    return CashFlowReport(
        as_of_date=cutoff,
        period_start=pnl.period_start,
        operating_cash_flow=operating_items,
        net_cash_from_operating=_quantize(net_cash_operating),
        investing_cash_flow=[],
        net_cash_from_investing=Decimal("0.0000"),
        financing_cash_flow=[],
        net_cash_from_financing=Decimal("0.0000"),
        net_change_in_cash=_quantize(net_cash_operating),
        beginning_cash_balance=Decimal("0.0000"),
        ending_cash_balance=_quantize(ending_cash),
    )


async def get_dashboard_kpis(db: AsyncSession) -> AccountingDashboardKPIs:
    """High-level financial KPIs for the dashboard."""
    escrow_stmt = (
        select(func.coalesce(func.sum(JournalLine.credit), 0))
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code == "2010", JournalEntry.status == JournalEntryStatus.POSTED)
    )
    gmv = _quantize(Decimal(str((await db.execute(escrow_stmt)).scalar_one())))

    rev_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit), 0),
            func.coalesce(func.sum(JournalLine.debit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.type == AccountType.REVENUE, JournalEntry.status == JournalEntryStatus.POSTED)
    )
    rev_res = (await db.execute(rev_stmt)).one()
    net_revenue = _quantize(Decimal(str(rev_res[0])) - Decimal(str(rev_res[1])))

    holding_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit), 0),
            func.coalesce(func.sum(JournalLine.debit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code.in_(["2010", "2020"]), JournalEntry.status == JournalEntryStatus.POSTED)
    )
    h_res = (await db.execute(holding_stmt)).one()
    escrow_holding = _quantize(Decimal(str(h_res[0])) - Decimal(str(h_res[1])))

    payout_stmt = select(func.coalesce(func.sum(VendorPayout.amount), 0)).where(
        VendorPayout.status.in_([VendorPayoutStatus.PENDING, VendorPayoutStatus.PROCESSING, VendorPayoutStatus.APPROVED])
    )
    pending_payouts = _quantize(Decimal(str((await db.execute(payout_stmt)).scalar_one())))

    bank_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code == "1010", JournalEntry.status == JournalEntryStatus.POSTED)
    )
    b_res = (await db.execute(bank_stmt)).one()
    bank_bal = _quantize(Decimal(str(b_res[0])) - Decimal(str(b_res[1])))

    gw_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code == "1020", JournalEntry.status == JournalEntryStatus.POSTED)
    )
    gw_res = (await db.execute(gw_stmt)).one()
    gw_bal = _quantize(Decimal(str(gw_res[0])) - Decimal(str(gw_res[1])))

    pay_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit), 0),
            func.coalesce(func.sum(JournalLine.debit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code == "2030", JournalEntry.status == JournalEntryStatus.POSTED)
    )
    pay_res = (await db.execute(pay_stmt)).one()
    merchant_payables = _quantize(Decimal(str(pay_res[0])) - Decimal(str(pay_res[1])))

    res_stmt = (
        select(
            func.coalesce(func.sum(JournalLine.credit), 0),
            func.coalesce(func.sum(JournalLine.debit), 0),
        )
        .join(Account, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(Account.code == "1040", JournalEntry.status == JournalEntryStatus.POSTED)
    )
    res_out = (await db.execute(res_stmt)).one()
    reserves_held = _quantize(Decimal(str(res_out[0])) - Decimal(str(res_out[1])))

    liquid_funds = bank_bal + gw_bal
    short_term_liabilities = escrow_holding + merchant_payables
    health_ratio = (
        float(liquid_funds / short_term_liabilities) if short_term_liabilities > 0 else 1.0
    )

    total_je = (await db.execute(select(func.count(JournalEntry.id)))).scalar_one()

    return AccountingDashboardKPIs(
        gmv=gmv,
        net_platform_revenue=net_revenue,
        escrow_holding=escrow_holding,
        pending_vendor_payouts=pending_payouts,
        settlement_health_ratio=round(health_ratio, 2),
        operating_bank_balance=bank_bal,
        gateway_clearing_balance=gw_bal,
        total_merchant_payables=merchant_payables,
        active_reserve_holds=reserves_held,
        total_journal_entries_count=total_je,
    )


# ============================================================================
# 6. CSV EXPORTS
# ============================================================================

async def export_general_ledger_csv(db: AsyncSession, start_date: datetime | None = None, end_date: datetime | None = None) -> str:
    """Generates General Ledger CSV string with audit trail references."""
    stmt = (
        select(JournalEntry, JournalLine, Account)
        .join(JournalLine, JournalEntry.id == JournalLine.journal_entry_id)
        .join(Account, JournalLine.account_id == Account.id)
        .order_by(JournalEntry.posted_at.desc(), JournalEntry.entry_number)
    )
    if start_date:
        stmt = stmt.where(JournalEntry.posted_at >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.posted_at <= end_date)

    rows = (await db.execute(stmt)).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Entry Number", "Posted At", "Transaction Type", "Reference ID", "Account Code",
        "Account Name", "Sub-Ledger Type", "Sub-Ledger ID", "Debit", "Credit", "Currency",
        "Narration", "Line Description", "Status"
    ])

    for entry, line, account in rows:
        writer.writerow([
            entry.entry_number,
            entry.posted_at.isoformat(),
            entry.transaction_type.value,
            entry.reference_id or "",
            account.code,
            account.name,
            line.sub_ledger_type.value,
            line.sub_ledger_id or "",
            str(line.debit),
            str(line.credit),
            line.currency,
            entry.narration,
            line.description or "",
            entry.status.value,
        ])

    return output.getvalue()
