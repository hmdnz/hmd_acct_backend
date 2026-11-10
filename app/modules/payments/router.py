import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_admin, get_current_customer, get_current_rider, get_current_seller
from app.modules.auth.models import Admin, Rider, Seller, User
from app.modules.payments import schemas, service
from app.modules.payments.models import (
    Payment, PaymentDiscrepancy, PaymentDiscrepancyStatus, PaymentProvider, PaymentProviderEvent, Payout, PayoutStatus, Refund,
)
from app.modules.payments.provider import extract_event, verify_webhook_signature

router = APIRouter(prefix="/payments", tags=["payments"])
seller_router = APIRouter(prefix="/seller/payments", tags=["seller-payments"])
rider_router = APIRouter(prefix="/rider/payments", tags=["rider-payments"])
admin_router = APIRouter(prefix="/admin/payments", tags=["admin-payments"])


# =====================================================================
# Customers: pay, verify, ask for a refund
# =====================================================================

@router.post("/initialize", response_model=schemas.PaymentOut)
async def initialize(payload: schemas.InitializePaymentRequest, user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    """Start a payment for a marketplace order (order_id) or a food order (food_order_id). Open `checkout_url` to pay."""
    return await service.initialize_payment(db, user, payload)


@router.post("/verify", response_model=schemas.PaymentOut)
async def verify(payload: schemas.VerifyPaymentRequest, user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    """Call this when the customer comes back from the checkout page. The webhook does the same job if the app never calls."""
    owner = (await db.execute(select(Payment.customer_id).where(Payment.transaction_ref == payload.transaction_ref))).scalar_one_or_none()
    if owner is None or owner != user.id:
        raise HTTPException(404, "Payment not found")
    return await service.verify_and_settle(db, payload.transaction_ref)


@router.get("/{payment_id}", response_model=schemas.PaymentOut)
async def payment_detail(payment_id: uuid.UUID, user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    payment = await db.get(Payment, payment_id)
    if not payment or payment.customer_id != user.id:
        raise HTTPException(404, "Payment not found")
    return payment



# =====================================================================
# Tax Invoices & Payment Receipts (HTML Print & JSON)
# =====================================================================
from fastapi.responses import HTMLResponse
from app.modules.payments.receipt_service import ReceiptService

@router.get("/{payment_id}/receipt")
async def get_payment_receipt(payment_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Returns structured JSON tax invoice and payment receipt data with Naira (₦) amounts."""
    receipt_service = ReceiptService(db)
    return await receipt_service.get_receipt_data(payment_id)

@router.get("/{payment_id}/receipt/html", response_class=HTMLResponse)
async def get_payment_receipt_html(payment_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Returns an A4 print-ready HTML tax invoice with one-click PDF printing support."""
    receipt_service = ReceiptService(db)
    return HTMLResponse(content=await receipt_service.generate_receipt_html(payment_id))

@router.post("/refunds", response_model=schemas.RefundOut)
async def refund_request(payload: schemas.RefundCreate, user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    return await service.request_refund(db, user, payload)


# =====================================================================
# Provider webhooks (no login: they are checked by signature)
# =====================================================================

async def _webhook(provider: PaymentProvider, request: Request, db: AsyncSession, signature: str | None):
    raw = await request.body()
    if not verify_webhook_signature(provider, raw, signature):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    event_type, reference, event_id = extract_event(provider, payload)

    event = (await db.execute(
        select(PaymentProviderEvent).where(PaymentProviderEvent.provider == provider, PaymentProviderEvent.event_id == event_id)
    )).scalar_one_or_none()
    if event and event.processed:
        return {"received": True, "duplicate": True}
    if not event:
        event = PaymentProviderEvent(
            provider=provider, event_id=event_id, event_type=event_type, payment_reference=reference,
            payload_hash=hashlib.sha256(raw).hexdigest(), payload=raw.decode("utf-8", errors="replace"),
        )
        db.add(event)
        try:
            await db.commit()
        except IntegrityError:  # the same event arrived twice at the same moment
            await db.rollback()
            return {"received": True, "duplicate": True}

    try:
        await service.handle_provider_event(db, provider, event_type, reference)
    except HTTPException as exc:
        if exc.status_code in (404, 409):
            # asking again would give the same answer (unknown payment, amount mismatch already logged): stop retries
            event.processed = True
            event.processed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"received": True, "note": str(exc.detail)}
        raise  # e.g. 502: the provider will retry later
    event.processed = True
    event.processed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"received": True}


@router.post("/webhooks/paystack")
async def paystack_webhook(request: Request, x_paystack_signature: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    return await _webhook(PaymentProvider.PAYSTACK, request, db, x_paystack_signature)


@router.post("/webhooks/squad")
async def squad_webhook(
    request: Request, x_squad_signature: str | None = Header(default=None),
    x_squad_encrypted_body: str | None = Header(default=None), db: AsyncSession = Depends(get_db),
):
    return await _webhook(PaymentProvider.SQUAD, request, db, x_squad_encrypted_body or x_squad_signature)


# =====================================================================
# Sellers (and food shops): wallet, bank accounts, payouts
# =====================================================================

@seller_router.get("/wallet", response_model=schemas.WalletOut)
async def wallet(seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    w = await service.get_wallet(db, seller.id)
    await db.commit()  # the wallet row is created on first look
    return schemas.WalletOut(seller_id=seller.id, currency=w.currency, available_minor=w.available_minor, pending_minor=w.pending_minor, locked_minor=w.locked_minor)


@seller_router.get("/transactions", response_model=list[schemas.WalletEntryOut])
async def transactions(seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    rows = await service.list_wallet_entries(db, seller.id)
    await db.commit()
    return rows


@seller_router.get("/banks", response_model=list[schemas.BankOut])
async def banks(seller: Seller = Depends(get_current_seller)):
    return await service.list_banks()


@seller_router.get("/bank-accounts", response_model=list[schemas.BankAccountOut])
async def bank_accounts(seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    return await service.list_bank_accounts(db, seller.id)


@seller_router.post("/bank-accounts", response_model=schemas.BankAccountOut)
async def add_bank_account(payload: schemas.BankAccountCreate, seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    return await service.add_bank_account(db, seller, payload)


@seller_router.post("/bank-accounts/{account_id}/recheck-squad", response_model=schemas.BankAccountOut)
async def recheck_squad(account_id: uuid.UUID, seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    """Ask Squad again about this saved account, e.g. after Squad enables payouts for your merchant."""
    return await service.recheck_squad(db, seller, account_id)


@seller_router.post("/payouts", response_model=schemas.PayoutOut)
async def payout(payload: schemas.PayoutCreate, seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    """Withdraw money by hand. Only available when SELLER_PAYOUT_MODE=manual; in the default "auto" mode every order's
    money is sent to the seller's bank by itself once its hold is over."""
    if settings.SELLER_PAYOUT_MODE == "auto":
        raise HTTPException(409, "Your earnings are sent to your bank account automatically. There is nothing to withdraw.")
    return await service.create_payout(db, seller, payload)


@seller_router.get("/payouts", response_model=list[schemas.PayoutOut])
async def payouts(seller: Seller = Depends(get_current_seller), db: AsyncSession = Depends(get_db)):
    return await service.list_payouts(db, seller.id)


# =====================================================================
# Riders: earnings, bank account, payout history
# =====================================================================

@rider_router.get("/earnings", response_model=schemas.RiderEarningsOut)
async def rider_earnings(rider: Rider = Depends(get_current_rider), db: AsyncSession = Depends(get_db)):
    return await service.rider_earnings_summary(db, rider.id)


@rider_router.get("/banks", response_model=list[schemas.BankOut])
async def rider_banks(rider: Rider = Depends(get_current_rider)):
    return await service.list_banks()


@rider_router.get("/bank-accounts", response_model=list[schemas.RiderBankAccountOut])
async def rider_bank_accounts(rider: Rider = Depends(get_current_rider), db: AsyncSession = Depends(get_db)):
    return await service.list_rider_bank_accounts(db, rider.id)


@rider_router.post("/bank-accounts", response_model=schemas.RiderBankAccountOut)
async def rider_add_bank_account(payload: schemas.RiderBankAccountCreate, rider: Rider = Depends(get_current_rider), db: AsyncSession = Depends(get_db)):
    return await service.add_rider_bank_account(db, rider, payload)


@rider_router.get("/payouts", response_model=list[schemas.RiderPayoutOut])
async def rider_payouts(rider: Rider = Depends(get_current_rider), db: AsyncSession = Depends(get_db)):
    return await service.list_rider_payouts(db, rider_id=rider.id)


# =====================================================================
# Admin
# =====================================================================

@admin_router.get("/refunds", response_model=list[schemas.RefundOut])
async def admin_refunds(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Refund).order_by(Refund.created_at.desc()).limit(200))).scalars().all()


@admin_router.post("/refunds/{refund_id}/approve", response_model=schemas.RefundOut)
async def approve_refund(refund_id: uuid.UUID, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await service.process_refund(db, refund_id, admin.id, True)


@admin_router.post("/refunds/{refund_id}/reject", response_model=schemas.RefundOut)
async def reject_refund(refund_id: uuid.UUID, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await service.process_refund(db, refund_id, admin.id, False)


@admin_router.post("/{payment_id}/refund", response_model=schemas.RefundOut)
async def refund_payment(payment_id: uuid.UUID, payload: schemas.AdminRefundRequest, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Refund a payment right now (full unless amount_minor is given). Use for duplicate or cancelled-order payments."""
    return await service.admin_refund_payment(db, admin.id, payment_id, payload)


@admin_router.get("/payouts", response_model=list[schemas.PayoutOut])
async def admin_payouts(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Payout).order_by(Payout.created_at.desc()).limit(200))).scalars().all()


@admin_router.get("/discrepancies", response_model=list[schemas.DiscrepancyOut])
async def admin_discrepancies(open_only: bool = True, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    stmt = select(PaymentDiscrepancy).order_by(PaymentDiscrepancy.created_at.desc()).limit(200)
    if open_only:
        stmt = stmt.where(PaymentDiscrepancy.status == PaymentDiscrepancyStatus.OPEN)
    return (await db.execute(stmt)).scalars().all()


@admin_router.post("/discrepancies/{discrepancy_id}/resolve", response_model=schemas.DiscrepancyOut)
async def resolve_discrepancy(discrepancy_id: uuid.UUID, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    item = await db.get(PaymentDiscrepancy, discrepancy_id)
    if not item:
        raise HTTPException(404, "Not found")
    item.status = PaymentDiscrepancyStatus.RESOLVED
    item.resolved_by = admin.id
    item.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(item)
    return item


@admin_router.get("/rider-payouts", response_model=list[schemas.RiderPayoutOut])
async def admin_rider_payouts(status: PayoutStatus | None = None, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await service.list_rider_payouts(db, status=status, limit=200)


@admin_router.post("/rider-payouts/run", response_model=list[schemas.RiderPayoutOut])
async def run_rider_payouts_now(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Prepare today's rider payouts now (the daily job does this automatically). They wait for approval unless RIDER_PAYOUT_AUTO is on."""
    return await service.create_daily_rider_payouts(db)


@admin_router.post("/rider-payouts/approve-all", response_model=list[schemas.RiderPayoutOut])
async def approve_all_rider_payouts(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    sent = []
    for payout in await service.list_rider_payouts(db, status=PayoutStatus.REQUESTED, limit=500):
        try:
            sent.append(await service.send_rider_payout(db, payout.id, admin.id))
        except HTTPException:
            continue
    return sent


@admin_router.post("/rider-payouts/{payout_id}/approve", response_model=schemas.RiderPayoutOut)
async def approve_rider_payout(payout_id: uuid.UUID, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    return await service.send_rider_payout(db, payout_id, admin.id)


@admin_router.post("/squad/lookup-check")
async def squad_lookup_check(payload: schemas.SquadLookupRequest, admin: Admin = Depends(get_current_admin)):
    """Diagnostic: run Squad's account lookup exactly as the app does and show Squad's answer or the exact error."""
    return await service.admin_squad_lookup(payload.bank_code, payload.account_number)


@admin_router.post("/seller-payouts/run")
async def run_seller_payouts(admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)):
    """Run the whole seller-money cycle now (release finished holds, send automatic payouts, settle Squad transfers).
    The scheduled job does the same every 10 minutes; use this to run it by hand or from an outside scheduler."""
    return await service.run_seller_payout_cycle(db)
