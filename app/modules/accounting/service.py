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
    PaymentTransactionStatus,
    ReconciliationStatus,
    RefundStatus,
    SubLedgerType,
    TaxRate,
    TransactionType,
    VendorPayoutStatus,
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
                if row.code.startswith("5"):
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

    pending_payouts = Decimal("0.0000")  # payouts are executed by hamads-dev, not here

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

# ============================================================================
# 7. E-COMMERCE BRIDGE WEBHOOK INGESTION (HMAC-SHA256 EVENTS)
# ============================================================================


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
