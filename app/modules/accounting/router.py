import hashlib
import hmac
import json
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.modules.accounting import paystack, schemas, service
from app.modules.accounting.models import (
    Account,
    AccountingSettings,
    AccountType,
    ExpenseBill,
    FiscalPeriod,
    FiscalPeriodStatus,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
    PaymentTransaction,
    PaymentTransactionStatus,
    SubLedgerType,
    TransactionType,
    VendorPayout,
    VendorPayoutStatus,
)
from app.modules.accounting.seed import seed_chart_of_accounts
from app.modules.auth.dependencies import get_current_admin, get_current_seller
from app.modules.auth.models import Admin, Seller

from app.core.config import settings
from app.db.session import get_db
router = APIRouter(tags=["accounting"])


# ============================================================================
# 1. CHART OF ACCOUNTS (ADMIN)
# ============================================================================

@router.get("/admin/accounting/accounts", response_model=list[schemas.AccountOut])
async def list_accounts(
    account_type: AccountType | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = select(Account).order_by(Account.code)
    if account_type:
        stmt = stmt.where(Account.type == account_type)
    if is_active is not None:
        stmt = stmt.where(Account.is_active == is_active)

    res = await db.execute(stmt)
    return res.scalars().all()


@router.get("/admin/accounting/accounts/tree", response_model=list[schemas.AccountTreeNode])
@router.get("/accounts/tree", response_model=list[schemas.AccountTreeNode])
async def get_account_tree(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    result = await db.execute(select(Account).order_by(Account.code))
    accounts = result.scalars().all()

    by_parent: dict[uuid.UUID | None, list[Account]] = {}
    for acc in accounts:
        by_parent.setdefault(acc.parent_id, []).append(acc)

    def build_node(acc: Account) -> schemas.AccountTreeNode:
        data = {
            col.key: getattr(acc, col.key)
            for col in Account.__table__.columns
        }
        data["normal_balance"] = acc.normal_balance
        data["children"] = [build_node(child) for child in by_parent.get(acc.id, [])]
        return schemas.AccountTreeNode.model_validate(data)

    return [build_node(root) for root in by_parent.get(None, [])]

@router.post("/admin/accounting/accounts", response_model=schemas.AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: schemas.AccountCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    existing = (await db.execute(select(Account).where(Account.code == payload.code))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account code '{payload.code}' already exists.",
        )

    account = Account(
        code=payload.code,
        name=payload.name,
        type=payload.type,
        parent_id=payload.parent_id,
        is_active=payload.is_active,
        currency=payload.currency,
        description=payload.description,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.patch("/admin/accounting/accounts/{account_id}", response_model=schemas.AccountOut)
async def update_account(
    account_id: uuid.UUID,
    payload: schemas.AccountUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    account = await db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    if payload.name is not None:
        account.name = payload.name
    if payload.is_active is not None:
        account.is_active = payload.is_active
    if payload.description is not None:
        account.description = payload.description
    if payload.parent_id is not None:
        account.parent_id = payload.parent_id

    await db.commit()
    await db.refresh(account)
    return account


@router.post("/admin/accounting/accounts/seed", status_code=status.HTTP_200_OK)
async def seed_coa_endpoint(
    currency: str = Query("NGN", max_length=3),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    codes = await seed_chart_of_accounts(db)
    return {"message": "Chart of Accounts successfully seeded.", "accounts_count": len(codes)}


# ============================================================================
# 2. SETTINGS & FISCAL PERIODS (ADMIN)
# ============================================================================

@router.post("/accounting/webhooks/ecommerce", status_code=status.HTTP_200_OK)
async def ecommerce_bridge_webhook(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    db: AsyncSession = Depends(get_db),
):
    """
    Receives HMAC-SHA256 signed financial events from hamads-dev accounting_bridge.py.
    """
    secret = getattr(settings, "ACCOUNTING_WEBHOOK_SECRET", None)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ACCOUNTING_WEBHOOK_SECRET is not configured",
        )

    raw_body = await request.body()
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not x_signature_256 or not hmac.compare_digest(expected_sig, x_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    payload = json.loads(raw_body.decode("utf-8"))
    event_type = payload.get("event_type", "")
    data = payload.get("data") or {}

    return await service.ingest_ecommerce_webhook_event(db, event_type, data)

@router.get("/admin/accounting/settings", response_model=schemas.AccountingSettingsOut)
async def get_accounting_settings(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    settings_obj = (await db.execute(select(AccountingSettings))).scalar_one_or_none()
    if not settings_obj:
        await seed_chart_of_accounts(db)
        settings_obj = (await db.execute(select(AccountingSettings))).scalar_one()
    return settings_obj


@router.patch("/admin/accounting/settings", response_model=schemas.AccountingSettingsOut)
async def update_accounting_settings(
    payload: schemas.AccountingSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    settings_obj = (await db.execute(select(AccountingSettings))).scalar_one_or_none()
    if not settings_obj:
        raise HTTPException(status_code=404, detail="Settings not found. Please seed first.")

    if payload.legal_entity_name is not None:
        settings_obj.legal_entity_name = payload.legal_entity_name
    if payload.functional_currency is not None:
        settings_obj.functional_currency = payload.functional_currency
    if payload.tax_identification_number is not None:
        settings_obj.tax_identification_number = payload.tax_identification_number
    if payload.standard_commission_rate is not None:
        settings_obj.standard_commission_rate = payload.standard_commission_rate
    if payload.standard_reserve_rate is not None:
        settings_obj.standard_reserve_rate = payload.standard_reserve_rate
    if payload.reserve_hold_days is not None:
        settings_obj.reserve_hold_days = payload.reserve_hold_days
    if payload.require_dual_approval_payouts is not None:
        settings_obj.require_dual_approval_payouts = payload.require_dual_approval_payouts

    await db.commit()
    await db.refresh(settings_obj)
    return settings_obj


@router.get("/admin/accounting/periods", response_model=list[schemas.FiscalPeriodOut])
async def list_fiscal_periods(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = select(FiscalPeriod).order_by(desc(FiscalPeriod.start_date))
    return (await db.execute(stmt)).scalars().all()


@router.post("/admin/accounting/periods", response_model=schemas.FiscalPeriodOut, status_code=status.HTTP_201_CREATED)
async def create_fiscal_period(
    payload: schemas.FiscalPeriodCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    period = FiscalPeriod(
        name=payload.name,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=FiscalPeriodStatus.OPEN,
    )
    db.add(period)
    await db.commit()
    await db.refresh(period)
    return period


@router.post("/admin/accounting/periods/{period_id}/lock", response_model=schemas.FiscalPeriodOut)
async def lock_fiscal_period(
    period_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    period = await db.get(FiscalPeriod, period_id)
    if not period:
        raise HTTPException(status_code=404, detail="Period not found")
    period.status = FiscalPeriodStatus.LOCKED
    period.closed_at = service._utc_now()
    period.closed_by = admin.id
    await db.commit()
    await db.refresh(period)
    return period


# ============================================================================
# 3. GENERAL LEDGER & MANUAL ENTRIES (ADMIN)
# ============================================================================

@router.get("/admin/accounting/journal-entries", response_model=list[schemas.JournalEntryOut])
async def list_journal_entries(
    transaction_type: TransactionType | None = None,
    status_filter: JournalEntryStatus | None = None,
    reference_id: str | None = None,
    account_id: uuid.UUID | None = None,
    sub_ledger_id: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .order_by(desc(JournalEntry.posted_at), desc(JournalEntry.created_at))
    )
    if transaction_type:
        stmt = stmt.where(JournalEntry.transaction_type == transaction_type)
    if status_filter:
        stmt = stmt.where(JournalEntry.status == status_filter)
    if reference_id:
        stmt = stmt.where(JournalEntry.reference_id == reference_id)
    if start_date:
        stmt = stmt.where(JournalEntry.posted_at >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.posted_at <= end_date)
    if account_id:
        stmt = stmt.join(JournalEntry.lines).where(JournalLine.account_id == account_id)
    if sub_ledger_id:
        stmt = stmt.join(JournalEntry.lines).where(JournalLine.sub_ledger_id == sub_ledger_id)

    stmt = stmt.limit(limit).offset(offset)
    res = await db.execute(stmt)
    entries = res.scalars().all()

    result: list[schemas.JournalEntryOut] = []
    for entry in entries:
        out_lines: list[schemas.JournalLineOut] = []
        tot_debit = Decimal("0.0000")
        tot_credit = Decimal("0.0000")

        for line in entry.lines:
            tot_debit += line.debit
            tot_credit += line.credit
            out_lines.append(
                schemas.JournalLineOut(
                    id=line.id,
                    account_id=line.account_id,
                    account_code=line.account.code if line.account else None,
                    account_name=line.account.name if line.account else None,
                    sub_ledger_type=line.sub_ledger_type,
                    sub_ledger_id=line.sub_ledger_id,
                    debit=line.debit,
                    credit=line.credit,
                    currency=line.currency,
                    base_debit=line.base_debit,
                    base_credit=line.base_credit,
                    description=line.description,
                )
            )

        result.append(
            schemas.JournalEntryOut(
                id=entry.id,
                entry_number=entry.entry_number,
                idempotency_key=entry.idempotency_key,
                transaction_type=entry.transaction_type,
                reference_id=entry.reference_id,
                narration=entry.narration,
                status=entry.status,
                currency=entry.currency,
                posted_at=entry.posted_at,
                created_by=entry.created_by,
                approved_by=entry.approved_by,
                reverses_entry_id=entry.reverses_entry_id,
                reversed_by_id=entry.reversed_by_id,
                lines=out_lines,
                total_debit=tot_debit,
                total_credit=tot_credit,
            )
        )
    return result


@router.post("/admin/accounting/journal-entries", response_model=schemas.JournalEntryOut, status_code=status.HTTP_201_CREATED)
async def create_manual_journal_entry(
    payload: schemas.JournalEntryCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    entry = await service.post_journal_entry(db, payload, created_by=admin.id, approved_by=admin.id)
    await db.commit()
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry.id)
    )
    loaded = (await db.execute(stmt)).scalar_one()

    return schemas.JournalEntryOut(
        id=loaded.id,
        entry_number=loaded.entry_number,
        idempotency_key=loaded.idempotency_key,
        transaction_type=loaded.transaction_type,
        reference_id=loaded.reference_id,
        narration=loaded.narration,
        status=loaded.status,
        currency=loaded.currency,
        posted_at=loaded.posted_at,
        created_by=loaded.created_by,
        approved_by=loaded.approved_by,
        reverses_entry_id=loaded.reverses_entry_id,
        reversed_by_id=loaded.reversed_by_id,
        lines=[
            schemas.JournalLineOut(
                id=l.id,
                account_id=l.account_id,
                account_code=l.account.code if l.account else None,
                account_name=l.account.name if l.account else None,
                sub_ledger_type=l.sub_ledger_type,
                sub_ledger_id=l.sub_ledger_id,
                debit=l.debit,
                credit=l.credit,
                currency=l.currency,
                base_debit=l.base_debit,
                base_credit=l.base_credit,
                description=l.description,
            )
            for l in loaded.lines
        ],
        total_debit=sum(l.debit for l in loaded.lines),
        total_credit=sum(l.credit for l in loaded.lines),
    )


@router.post("/admin/accounting/journal-entries/{entry_id}/reverse", response_model=schemas.JournalEntryOut)
async def reverse_entry(
    entry_id: uuid.UUID,
    reason: str = Query(..., min_length=5),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    reversal = await service.reverse_journal_entry(db, entry_id, reason, admin_id=admin.id)
    return await list_journal_entries(reference_id=reversal.reference_id, limit=1, db=db, admin=admin)


# ============================================================================
# 4. LIFECYCLE EVENT TRIGGER ENDPOINTS (ADMIN / SYSTEM)
# ============================================================================

@router.post("/admin/accounting/events/order-paid", status_code=status.HTTP_201_CREATED)
async def trigger_order_paid(
    payload: schemas.OrderPaymentEvent,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    entry = await service.post_order_payment(db, payload)
    return {"message": "Order payment escrow posted.", "entry_number": entry.entry_number}


@router.post("/admin/accounting/events/escrow-released", status_code=status.HTTP_201_CREATED)
async def trigger_escrow_release(
    payload: schemas.EscrowReleaseEvent,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    entry = await service.post_escrow_release(db, payload)
    return {"message": "Escrow released to vendor payable.", "entry_number": entry.entry_number}


@router.post("/admin/accounting/events/refund", status_code=status.HTTP_201_CREATED)
async def trigger_refund(
    payload: schemas.RefundEvent,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    entry = await service.post_order_refund(db, payload)
    return {"message": "Refund posted to ledger.", "entry_number": entry.entry_number}


@router.post("/admin/accounting/events/gateway-settlement", status_code=status.HTTP_201_CREATED)
async def trigger_gateway_settlement(
    payload: schemas.GatewaySettlementEvent,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    entry = await service.post_gateway_settlement(db, payload)
    return {"message": "Gateway settlement reconciled.", "entry_number": entry.entry_number}


# ============================================================================
# 5. VENDOR PAYOUT MANAGEMENT & SUBLEDGERS (ADMIN & SELLER)
# ============================================================================

@router.get("/admin/accounting/vendors/{vendor_id}/ledger", response_model=schemas.VendorSubLedgerSummary)
async def admin_get_vendor_ledger(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.get_vendor_subledger_summary(db, vendor_id)


@router.get("/admin/accounting/payouts", response_model=list[schemas.VendorPayoutOut])
async def admin_list_payouts(
    status_filter: VendorPayoutStatus | None = None,
    vendor_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = (
        select(VendorPayout, Seller.brand_name)
        .join(Seller, VendorPayout.vendor_id == Seller.id)
        .order_by(desc(VendorPayout.created_at))
    )
    if status_filter:
        stmt = stmt.where(VendorPayout.status == status_filter)
    if vendor_id:
        stmt = stmt.where(VendorPayout.vendor_id == vendor_id)

    rows = (await db.execute(stmt.limit(limit).offset(offset))).all()
    out = []
    for p, brand in rows:
        item = schemas.VendorPayoutOut.model_validate(p)
        item.vendor_name = brand
        out.append(item)
    return out


@router.post("/admin/accounting/payouts/{payout_id}/complete", response_model=schemas.VendorPayoutOut)
async def admin_complete_payout(
    payout_id: uuid.UUID,
    payload: schemas.VendorPayoutProcess,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.complete_vendor_payout(db, payout_id, payload.gateway_reference)


# --- SELLER RESTRICTED ENDPOINTS (TENANT ISOLATION) ---

@router.get("/seller/accounting/my-ledger", response_model=schemas.VendorSubLedgerSummary)
async def seller_get_my_ledger(
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    """Enforces strict tenant isolation: sellers can ONLY view their own statement."""
    return await service.get_vendor_subledger_summary(db, seller.id)


@router.get("/seller/accounting/my-payouts", response_model=list[schemas.VendorPayoutOut])
async def seller_list_my_payouts(
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    stmt = (
        select(VendorPayout)
        .where(VendorPayout.vendor_id == seller.id)
        .order_by(desc(VendorPayout.created_at))
    )
    payouts = (await db.execute(stmt)).scalars().all()
    out = []
    for p in payouts:
        item = schemas.VendorPayoutOut.model_validate(p)
        item.vendor_name = seller.brand_name
        out.append(item)
    return out


@router.post("/seller/accounting/request-payout", response_model=schemas.VendorPayoutOut, status_code=status.HTTP_201_CREATED)
async def seller_request_payout(
    payload: schemas.VendorPayoutCreate,
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    if payload.vendor_id != seller.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot request payout for another seller.")
    return await service.create_vendor_payout(db, payload)


# ============================================================================
# 6. OPERATING EXPENSES (ADMIN)
# ============================================================================

@router.post("/admin/accounting/expenses", response_model=schemas.ExpenseBillOut, status_code=status.HTTP_201_CREATED)
async def create_expense(
    payload: schemas.ExpenseBillCreate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.create_expense_bill(db, payload, admin_id=admin.id)


@router.post("/admin/accounting/expenses/{bill_id}/pay", response_model=schemas.ExpenseBillOut)
async def pay_expense(
    bill_id: uuid.UUID,
    payment_method_code: str = Query("1010", description="Bank cash code (1010)"),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.pay_expense_bill(db, bill_id, payment_method_code=payment_method_code)


# ============================================================================
# 7. FINANCIAL STATEMENTS & CSV EXPORT (ADMIN)
# ============================================================================

@router.get("/admin/accounting/reports/dashboard-kpis", response_model=schemas.AccountingDashboardKPIs)
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.get_dashboard_kpis(db)


@router.get("/admin/accounting/reports/trial-balance", response_model=schemas.TrialBalanceReport)
async def get_trial_balance(
    as_of_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.generate_trial_balance(db, as_of_date)


@router.get("/admin/accounting/reports/profit-and-loss", response_model=schemas.ProfitAndLossReport)
async def get_profit_and_loss(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.generate_profit_and_loss(db, start_date, end_date)


@router.get("/admin/accounting/reports/balance-sheet", response_model=schemas.BalanceSheetReport)
async def get_balance_sheet(
    as_of_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.generate_balance_sheet(db, as_of_date)


@router.get("/admin/accounting/reports/cash-flow", response_model=schemas.CashFlowReport)
async def get_cash_flow(
    as_of_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    return await service.generate_cash_flow_statement(db, as_of_date)


@router.get("/admin/accounting/reports/export/general-ledger-csv")
async def export_gl_csv(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    csv_data = await service.export_general_ledger_csv(db, start_date, end_date)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=general_ledger_{datetime.utcnow().strftime('%Y%m%d')}.csv"},
    )


# ============================================================================
# 8. PAYMENT PROVIDER WEBHOOK (AUTHORITATIVE PAYSTACK HANDLER)
# ============================================================================

@router.post("/accounting/webhooks/paystack", status_code=status.HTTP_200_OK)
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Authoritative Paystack Webhook Handler with HMAC-SHA512 Signature Verification.
    """
    raw_body = await request.body()
    if not paystack.verify_paystack_webhook_signature(raw_body, x_paystack_signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    payload = json.loads(raw_body.decode("utf-8"))
    event = payload.get("event")
    data = payload.get("data", {})

    if event == "charge.success":
        reference = data.get("reference")
        amount = Decimal(str(data.get("amount", 0))) / Decimal("100.0")  # kobo to naira
        metadata = data.get("metadata", {})
        order_id_str = metadata.get("order_id")
        customer_id_str = metadata.get("customer_id")

        if order_id_str and customer_id_str:
            order_id = uuid.UUID(order_id_str)
            customer_id = uuid.UUID(customer_id_str)

            event_req = schemas.OrderPaymentEvent(
                order_id=order_id,
                total_amount=amount,
                shipping_fee=Decimal(str(metadata.get("shipping_fee", "0.0"))),
                gateway_reference=reference,
                customer_id=customer_id,
                currency=data.get("currency", "NGN"),
                provider="paystack",
                idempotency_key=f"PAYSTACK-CHARGE-{reference}",
            )
            await service.post_order_payment(db, event_req)

    return {"status": "success"}
