from app.modules.payments.accounting_bridge import AccountingBridge
"""Payments: paying for orders, seller wallets and payouts, refunds, and daily rider pay.

Money is stored in kobo ("minor" units) everywhere in this module.

How the pieces connect
  * Marketplace order  -> POST /payments/initialize {order_id}      -> Paystack/Squad checkout
  * Food order         -> POST /payments/initialize {food_order_id} -> same checkout
  * Provider webhook / verify -> verify_and_settle(): marks the order paid and credits the seller's
    wallet as *pending* (marketplace: seller's part of the order; food: the food subtotal less commission).
  * Marketplace delivery confirmed -> on_seller_order_delivered(): money becomes withdrawable after
    PAYOUT_HOLD_HOURS. Food order completed -> on_food_order_completed(): after FOOD_HOLD_HOURS (default 0),
    and the rider's delivery fee is recorded as a RiderEarning.
  * Cancelled after payment -> a Refund (status "requested") is opened for an admin to approve.
  * Riders are paid once a day by create_daily_rider_payouts() (see tasks.py).

Functions named on_* do NOT commit: the caller commits, so the money change and the order change
happen in one transaction.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.auth.models import Rider, Seller, User
from app.modules.food.models import FoodOrder, FoodOrderStatus, FoodVendor, FulfilmentType
from app.modules.catalog import inventory
from app.modules.orders.models import Order, OrderItem, OrderStatus, SellerOrder, SellerOrderStatus
from app.modules.notifications import events as ev, service as notifications
from app.modules.payments import bank_codes, schemas
from app.modules.payments.models import (
    AllocationStatus, BankAccountStatus, Payment, PaymentAllocation, PaymentDiscrepancy, PaymentDiscrepancyStatus, PaymentProvider,
    PaymentStatus, Payout, PayoutStatus, Refund, RefundStatus, RiderBankAccount, RiderEarning, RiderPayout,
    SellerBankAccount, SellerWallet, WalletEntry, WalletEntryType,
)
from app.modules.payments.provider import FAILED_STATUSES, SUCCESS_STATUSES, PaymentProviderClient, ProviderError
from app.modules.payments.security import decrypt, encrypt, hash_value

log = logging.getLogger(__name__)

LIVE_PAYOUT_STATES = (PayoutStatus.REQUESTED, PayoutStatus.APPROVED, PayoutStatus.PROCESSING, PayoutStatus.SUCCESSFUL)
PENDING_REUSE_MINUTES = 30  # a customer who taps "pay" again within this time gets the same checkout link
OPEN_REFUND_STATES = (RefundStatus.REQUESTED, RefundStatus.APPROVED, RefundStatus.PROCESSING)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ref(prefix: str, lower: bool = False) -> str:
    ref = f"{prefix}-{uuid.uuid4().hex[:20]}"
    return ref.lower() if lower else ref


def naira_to_minor(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def minor_to_naira(amount: int) -> Decimal:
    return Decimal(amount) / Decimal(100)


def _share(gross: int, rate: Decimal) -> int:
    return int((Decimal(gross) * Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def commission_for(gross: int) -> int:
    return _share(gross, settings.PLATFORM_COMMISSION_RATE)


def food_commission_for(gross: int) -> int:
    return _share(gross, settings.FOOD_COMMISSION_RATE)


def _data(response: dict[str, Any]) -> dict[str, Any]:
    inner = response.get("data")
    return inner if isinstance(inner, dict) else response


def _customer_email(user: User) -> str:
    return user.email or f"{user.phone_number.lstrip('+')}@{settings.PAYMENT_FALLBACK_EMAIL_DOMAIN}"


def _money(minor: int) -> str:
    """Kobo as naira text for messages, e.g. 700000 -> "₦7,000"."""
    naira = Decimal(minor) / 100
    return f"₦{naira:,.0f}" if naira == naira.to_integral_value() else f"₦{naira:,.2f}"


async def _flag(db: AsyncSession, payment: Payment, kind: str, details: str, expected: int | None = None, actual: int | None = None) -> None:
    """Record something an admin should look at (and tell the admins)."""
    db.add(PaymentDiscrepancy(
        payment_id=payment.id, provider=payment.provider, reference=payment.transaction_ref, discrepancy_type=kind,
        expected_amount_minor=expected, actual_amount_minor=actual, currency=payment.currency, details=details,
    ))
    await notifications.notify_admins(
        db, ev.NEEDS_ATTENTION, "A payment needs a look", details[:300],
        {"kind": kind, "payment_id": str(payment.id), "reference": payment.transaction_ref},
    )


# ---------------------------------------------------------------------------
# seller wallet
# ---------------------------------------------------------------------------

async def get_or_create_wallet(db: AsyncSession, seller_id: uuid.UUID, currency: str = "NGN", lock: bool = False) -> SellerWallet:
    stmt = select(SellerWallet).where(SellerWallet.seller_id == seller_id, SellerWallet.currency == currency)
    if lock:
        # populate_existing: re-read the row so we never add to a stale balance
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if not wallet:
        wallet = SellerWallet(seller_id=seller_id, currency=currency)
        db.add(wallet)
        await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
    return wallet


async def _wallet_entry(
    db: AsyncSession, wallet: SellerWallet, *, entry_type: WalletEntryType, direction: str, amount: int, reference: str,
    description: str, payment_id=None, allocation_id=None, payout_id=None, refund_id=None, order_id=None,
    seller_order_id=None, food_order_id=None, affect_lifetime: bool = True,
):
    if amount <= 0:
        return None
    entry = WalletEntry(
        wallet_id=wallet.id, seller_id=wallet.seller_id, entry_type=entry_type, direction=direction, amount_minor=amount,
        currency=wallet.currency, payment_id=payment_id, allocation_id=allocation_id, payout_id=payout_id,
        refund_id=refund_id, order_id=order_id, seller_order_id=seller_order_id, food_order_id=food_order_id,
        reference=reference, description=description,
    )
    db.add(entry)
    if affect_lifetime:
        if direction == "credit":
            wallet.lifetime_credits_minor += amount
        else:
            wallet.lifetime_debits_minor += amount
    return entry


async def get_wallet(db: AsyncSession, seller_id: uuid.UUID) -> SellerWallet:
    return await get_or_create_wallet(db, seller_id)


async def list_wallet_entries(db: AsyncSession, seller_id: uuid.UUID, limit: int = 100):
    wallet = await get_or_create_wallet(db, seller_id)
    return (await db.execute(
        select(WalletEntry).where(WalletEntry.wallet_id == wallet.id).order_by(WalletEntry.created_at.desc()).limit(limit)
    )).scalars().all()


# ---------------------------------------------------------------------------
# paying: start a payment
# ---------------------------------------------------------------------------

async def initialize_payment(db: AsyncSession, user: User, payload: schemas.InitializePaymentRequest) -> Payment:
    if payload.order_id is not None:
        order = (await db.execute(
            select(Order).where(Order.id == payload.order_id, Order.user_id == user.id).with_for_update()
        )).scalar_one_or_none()
        if not order:
            raise HTTPException(404, "Order not found")
        if order.status == OrderStatus.CANCELLED:
            raise HTTPException(400, "Cannot pay for a cancelled order")
        if order.status == OrderStatus.PAID:
            raise HTTPException(409, "Order is already paid")
        amount_minor = naira_to_minor(order.total_amount)
        target = Payment.order_id == order.id
        target_columns = {"order_id": order.id}
        metadata = {"order_id": str(order.id)}
    else:
        food_order = (await db.execute(
            select(FoodOrder).where(FoodOrder.id == payload.food_order_id, FoodOrder.customer_id == user.id).with_for_update()
        )).scalar_one_or_none()
        if not food_order:
            raise HTTPException(404, "Order not found")
        if food_order.status == FoodOrderStatus.CANCELLED:
            raise HTTPException(400, "Cannot pay for a cancelled order")
        if food_order.status != FoodOrderStatus.PENDING_PAYMENT:
            raise HTTPException(409, "Order is already paid")
        amount_minor = naira_to_minor(food_order.total_amount)
        target = Payment.food_order_id == food_order.id
        target_columns = {"food_order_id": food_order.id}
        metadata = {"food_order_id": str(food_order.id)}
    if amount_minor <= 0:
        raise HTTPException(400, "Nothing to pay for this order")

    existing = (await db.execute(
        select(Payment).where(target, Payment.status == PaymentStatus.PENDING).order_by(Payment.created_at.desc()).limit(1)
    )).scalars().first()
    if existing:
        recent = (_now() - existing.created_at) < timedelta(minutes=PENDING_REUSE_MINUTES)
        if recent and existing.provider == payload.provider and existing.checkout_url and existing.amount_minor == amount_minor:
            return existing
        # Superseded. If the customer still pays this old link, the webhook will still settle it.
        existing.status = PaymentStatus.CANCELLED

    payment = Payment(
        customer_id=user.id, provider=payload.provider, transaction_ref=_ref("HMPAY"), amount_minor=amount_minor,
        currency="NGN", status=PaymentStatus.PENDING, **target_columns,
    )
    db.add(payment)
    await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
    metadata["payment_id"] = str(payment.id)
    try:
        client = PaymentProviderClient(payload.provider)
        response = await client.initialize(
            email=_customer_email(user), amount_minor=amount_minor, reference=payment.transaction_ref,
            callback_url=payload.callback_url, currency=payment.currency, metadata=metadata,
        )
        data = _data(response)
        payment.checkout_url = data.get("authorization_url") or data.get("checkout_url") or data.get("auth_url")
        payment.provider_ref = data.get("reference") or data.get("transaction_ref") or payment.transaction_ref
        payment.provider_payload = json.dumps(response)
        if not payment.checkout_url:
            raise ProviderError("Provider did not return a checkout URL")
    except Exception as exc:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = str(exc)[:2000]
        await db.commit()
        raise HTTPException(502, "Could not start the payment. Please try again in a moment.")
    await db.commit()
    await db.refresh(payment)
    return payment


# ---------------------------------------------------------------------------
# paying: confirm a payment and credit the order
# ---------------------------------------------------------------------------

async def verify_and_settle(db: AsyncSession, transaction_ref: str) -> Payment:
    """Ask the provider whether this payment succeeded, and if so mark the order paid. Safe to call twice."""
    payment = (await db.execute(
        select(Payment).where(Payment.transaction_ref == transaction_ref).with_for_update()
    )).scalar_one_or_none()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status in (PaymentStatus.SUCCESSFUL, PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED):
        return payment
    try:
        response = await PaymentProviderClient(payment.provider).verify(transaction_ref)
    except Exception as exc:
        raise HTTPException(502, "Could not confirm the payment with the provider yet. Please try again in a moment.")
    data = _data(response)
    raw_status = str(data.get("status") or data.get("transaction_status") or "").lower()
    provider_amount = int(data.get("amount") or data.get("transaction_amount") or 0)
    provider_currency = str(data.get("currency") or data.get("transaction_currency_id") or payment.currency).upper()
    payment.provider_status = raw_status
    payment.provider_payload = json.dumps(response)

    if raw_status not in SUCCESS_STATUSES:
        if raw_status in FAILED_STATUSES:
            if payment.status != PaymentStatus.FAILED:
                await notifications.notify(
                    db, "customer", payment.customer_id, ev.PAYMENT_FAILED, "Payment failed",
                    f"Your payment of {_money(payment.amount_minor)} did not go through. You were not charged. Please try again.",
                    {"order_id": str(payment.order_id) if payment.order_id else None,
                     "food_order_id": str(payment.food_order_id) if payment.food_order_id else None, "payment_id": str(payment.id)},
                )
            payment.status = PaymentStatus.FAILED
        await db.commit()
        return payment

    if provider_amount != payment.amount_minor or provider_currency != payment.currency:
        await _flag(
            db, payment, "amount_or_currency_mismatch",
            f"Expected {payment.amount_minor} {payment.currency}; provider returned {provider_amount} {provider_currency}",
            payment.amount_minor, provider_amount,
        )
        await db.commit()
        raise HTTPException(409, "Payment verification mismatch; funds were not settled")

    payment.status = PaymentStatus.SUCCESSFUL
    payment.paid_at = _now()
    payment.provider_ref = data.get("reference") or data.get("transaction_ref") or payment.provider_ref
    if payment.provider == PaymentProvider.SQUAD:
        # a Squad refund needs Squad's own gateway reference, not ours
        payment.provider_ref = data.get("gateway_transaction_ref") or data.get("gateway_ref") or payment.provider_ref
    if payment.order_id is not None:
        await _settle_marketplace_order(db, payment)
    else:
        await _settle_food_order(db, payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def _settle_marketplace_order(db: AsyncSession, payment: Payment) -> None:
    order = (await db.execute(select(Order).where(Order.id == payment.order_id).with_for_update())).scalar_one()
    if order.status == OrderStatus.CANCELLED:
        await _flag(db, payment, "paid_cancelled_order", "The customer paid for an order that was already cancelled.")
        await _open_refund(db, payment, "Order was cancelled before the payment arrived", amount=payment.amount_minor, order_id=order.id)
        return
    if order.status == OrderStatus.PAID:
        await _flag(db, payment, "duplicate_payment", "This order was already paid by another payment.")
        await _open_refund(db, payment, "Duplicate payment for an order that was already paid", amount=payment.amount_minor, order_id=order.id)
        return
    seller_orders = (await db.execute(
        select(SellerOrder).where(SellerOrder.order_id == order.id).with_for_update()
    )).scalars().all()
    active = [so for so in seller_orders if so.status != SellerOrderStatus.CANCELLED]
    gross_sum = sum(naira_to_minor(so.subtotal) for so in active)
    # The customer also paid Hamads' delivery fee (zero when the sellers deliver). It is never allocated to a seller.
    delivery_fee = naira_to_minor(order.delivery_fee)
    if gross_sum + delivery_fee != payment.amount_minor:
        await _flag(
            db, payment, "allocation_mismatch",
            "The order's seller totals do not add up to the amount paid. The order was left unpaid for an admin to sort out.",
            payment.amount_minor, gross_sum + delivery_fee,
        )
        return
    if any(so.fulfilled_by == "hamads" for so in active):
        from app.modules.parcels import service as parcels_service  # local import: parcels imports this module
        if await parcels_service.create_for_paid_order(db, order, active) is None:
            await _flag(db, payment, "no_warehouse", "A Hamads-delivered order was paid but there is no active warehouse. Please set one up.")
            return
    order.status = OrderStatus.PAID
    # The goods are now sold: take them off the shelf (per variant where the product has options).
    paid_items = (await db.execute(
        select(OrderItem).where(OrderItem.seller_order_id.in_([so.id for so in active]))
    )).scalars().all()
    oversold = await inventory.take_stock(db, list(paid_items))
    if oversold:
        await _flag(db, payment, "oversold", "Not enough stock for: " + "; ".join(oversold))
    for so in active:
        gross = naira_to_minor(so.subtotal)
        commission = commission_for(gross)
        net = gross - commission
        allocation = PaymentAllocation(
            payment_id=payment.id, order_id=order.id, seller_order_id=so.id, seller_id=so.seller_id,
            gross_amount_minor=gross, platform_commission_minor=commission, seller_net_minor=net, status=AllocationStatus.PENDING,
        )
        db.add(allocation)
        await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
        wallet = await get_or_create_wallet(db, so.seller_id, payment.currency, lock=True)
        wallet.pending_minor += net
        await _wallet_entry(
            db, wallet, entry_type=WalletEntryType.SELLER_EARNING_PENDING, direction="credit", amount=net,
            reference=payment.transaction_ref, description=f"Pending earnings for order {order.id}",
            payment_id=payment.id, allocation_id=allocation.id, order_id=order.id, seller_order_id=so.id,
        )
        if so.fulfilled_by != "hamads":  # for Hamads-delivered orders the parcels module tells the seller what to do
            await notifications.notify(
                db, "seller", so.seller_id, ev.NEW_ORDER, "New order", f"You have a new paid order worth {_money(gross)}. Please start processing it.",
                {"seller_order_id": str(so.id), "order_id": str(order.id)},
            )
    await notifications.notify(
        db, "customer", payment.customer_id, ev.PAYMENT_CONFIRMED, "Payment received",
        f"We received your payment of {_money(payment.amount_minor)}. The sellers have been told to get your order ready.",
        {"order_id": str(order.id), "payment_id": str(payment.id)},
    )


async def _settle_food_order(db: AsyncSession, payment: Payment) -> None:
    fo = (await db.execute(select(FoodOrder).where(FoodOrder.id == payment.food_order_id).with_for_update())).scalar_one()
    if fo.status == FoodOrderStatus.CANCELLED:
        await _flag(db, payment, "paid_cancelled_order", "The customer paid for a food order that was already cancelled.")
        await _open_refund(db, payment, "Order was cancelled before the payment arrived", amount=payment.amount_minor, food_order_id=fo.id)
        return
    if fo.status != FoodOrderStatus.PENDING_PAYMENT:
        await _flag(db, payment, "duplicate_payment", "This food order was already paid by another payment.")
        await _open_refund(db, payment, "Duplicate payment for a food order that was already paid", amount=payment.amount_minor, food_order_id=fo.id)
        return
    if naira_to_minor(fo.total_amount) != payment.amount_minor:
        await _flag(
            db, payment, "allocation_mismatch", "The food order total no longer matches the amount paid. Left unpaid for an admin.",
            payment.amount_minor, naira_to_minor(fo.total_amount),
        )
        return
    vendor = await db.get(FoodVendor, fo.vendor_id)
    fo.status = FoodOrderStatus.PAID
    gross = naira_to_minor(fo.subtotal)
    commission = food_commission_for(gross)
    net = gross - commission
    allocation = PaymentAllocation(
        payment_id=payment.id, food_order_id=fo.id, seller_id=vendor.seller_id, gross_amount_minor=gross,
        platform_commission_minor=commission, seller_net_minor=net, status=AllocationStatus.PENDING,
    )
    db.add(allocation)
    await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
    wallet = await get_or_create_wallet(db, vendor.seller_id, payment.currency, lock=True)
    wallet.pending_minor += net
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.SELLER_EARNING_PENDING, direction="credit", amount=net,
        reference=payment.transaction_ref, description=f"Pending earnings for food order {fo.id}",
        payment_id=payment.id, allocation_id=allocation.id, food_order_id=fo.id,
    )
    await notifications.notify(
        db, "seller", vendor.seller_id, ev.NEW_FOOD_ORDER, "New food order",
        f"{fo.customer_name or 'A customer'} placed a paid order worth {_money(gross)}. Accept or reject it now.",
        {"food_order_id": str(fo.id)},
    )
    await notifications.notify(
        db, "customer", payment.customer_id, ev.PAYMENT_CONFIRMED, "Payment received",
        f"We received your payment of {_money(payment.amount_minor)}. {vendor.display_name} has your order.",
        {"food_order_id": str(fo.id), "payment_id": str(payment.id)},
    )
    # The delivery fee is not credited to anyone here: the rider's share is recorded when the delivery
    # completes (RiderEarning) and the rest stays with the platform.


# ---------------------------------------------------------------------------
# provider webhooks
# ---------------------------------------------------------------------------

CHARGE_EVENTS = {"charge.success", "charge_successful", "charge.successful"}
TRANSFER_EVENTS = {"transfer.success": "success", "transfer.failed": "failed", "transfer.reversed": "reversed"}


async def handle_provider_event(db: AsyncSession, provider: PaymentProvider, event_type: str, reference: str | None) -> str:
    """Act on one verified webhook event. Returns a short word describing what happened."""
    event = event_type.lower()
    if event in CHARGE_EVENTS:
        if not reference:
            return "ignored"
        known = (await db.execute(select(Payment.id).where(Payment.transaction_ref == reference))).first()
        if not known:
            return "ignored"
        await verify_and_settle(db, reference)
        return "settled"
    if event in TRANSFER_EVENTS and reference:
        found = await settle_transfer_event(db, reference, TRANSFER_EVENTS[event])
        return "transfer" if found else "ignored"
    return "ignored"


# ---------------------------------------------------------------------------
# releasing earnings to the seller's withdrawable balance
# ---------------------------------------------------------------------------

async def _release_allocation(db: AsyncSession, allocation: PaymentAllocation, now: datetime) -> bool:
    wallet = await get_or_create_wallet(db, allocation.seller_id, lock=True)
    net = allocation.seller_net_minor
    if wallet.pending_minor < net:
        return False  # the wallet does not add up: leave it for a human
    wallet.pending_minor -= net
    wallet.available_minor += net
    allocation.status = AllocationStatus.RELEASED
    allocation.released_at = now
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.SELLER_FUNDS_RELEASED, direction="credit", amount=net,
        reference=f"RELEASE-{allocation.id}", description="Earnings released and available to withdraw",
        allocation_id=allocation.id, order_id=allocation.order_id, seller_order_id=allocation.seller_order_id,
        food_order_id=allocation.food_order_id, affect_lifetime=False,
    )
    return True


async def on_seller_order_delivered(db: AsyncSession, so: SellerOrder) -> None:
    await AccountingBridge.on_order_delivered(db, so)
    """The customer confirmed delivery: start the hold clock on the seller's money. Caller commits."""
    allocation = (await db.execute(
        select(PaymentAllocation).where(PaymentAllocation.seller_order_id == so.id).with_for_update()
    )).scalar_one_or_none()
    if not allocation or allocation.status != AllocationStatus.PENDING:
        return
    delivered_at = so.delivered_at or _now()
    allocation.eligible_at = delivered_at + timedelta(hours=settings.PAYOUT_HOLD_HOURS)
    await notifications.notify(
        db, "seller", so.seller_id, ev.ORDER_DELIVERED, "Delivery confirmed",
        f"The customer confirmed delivery. Your {_money(allocation.seller_net_minor)} is paid out after {settings.PAYOUT_HOLD_HOURS} hours.",
        {"seller_order_id": str(so.id), "order_id": str(so.order_id)},
    )
    if allocation.eligible_at <= _now():
        await _release_allocation(db, allocation, _now())


async def on_food_order_completed(db: AsyncSession, order: FoodOrder) -> None:
    """A food order was delivered or collected. Releases the shop's money and records the rider's pay. Caller commits."""
    completed_at = order.completed_at or _now()
    allocation = (await db.execute(
        select(PaymentAllocation).where(PaymentAllocation.food_order_id == order.id).with_for_update()
    )).scalar_one_or_none()
    if allocation and allocation.status == AllocationStatus.PENDING:
        allocation.eligible_at = completed_at + timedelta(hours=settings.FOOD_HOLD_HOURS)
        await notifications.notify(
            db, "seller", allocation.seller_id, ev.ORDER_DELIVERED, "Order completed",
            f"The order is complete. Your {_money(allocation.seller_net_minor)} is paid out after {settings.FOOD_HOLD_HOURS} hours.",
            {"food_order_id": str(order.id)},
        )
        if allocation.eligible_at <= _now():
            await _release_allocation(db, allocation, _now())
    if order.fulfilment == FulfilmentType.DELIVERY and order.rider_id and order.rider_fee and order.rider_fee > 0:
        already = (await db.execute(select(RiderEarning.id).where(RiderEarning.food_order_id == order.id))).first()
        if not already:
            db.add(RiderEarning(
                rider_id=order.rider_id, food_order_id=order.id, amount_minor=naira_to_minor(order.rider_fee), earned_at=completed_at,
            ))


async def release_eligible_allocations(db: AsyncSession, limit: int = 200) -> int:
    """Safety net run every few minutes: release anything whose hold has passed."""
    now = _now()
    rows = (await db.execute(
        select(PaymentAllocation)
        .where(
            PaymentAllocation.status.in_([AllocationStatus.PENDING, AllocationStatus.ELIGIBLE]),
            PaymentAllocation.eligible_at.is_not(None),
            PaymentAllocation.eligible_at <= now,
        )
        .with_for_update(skip_locked=True).limit(limit)
    )).scalars().all()
    count = 0
    for allocation in rows:
        if allocation.seller_order_id is not None:
            so = await db.get(SellerOrder, allocation.seller_order_id)
            done = bool(so and so.status == SellerOrderStatus.DELIVERED)
        else:
            fo = await db.get(FoodOrder, allocation.food_order_id)
            done = bool(fo and fo.status == FoodOrderStatus.COMPLETED)
        if done and await _release_allocation(db, allocation, now):
            count += 1
    await db.commit()
    return count


# ---------------------------------------------------------------------------
# bank accounts and seller payouts
# ---------------------------------------------------------------------------

_BANK_CACHE: dict[str, Any] = {"at": None, "banks": []}


async def list_banks() -> list[dict[str, str]]:
    """Nigerian banks and their codes (from Paystack), cached for six hours."""
    cached_at = _BANK_CACHE["at"]
    if cached_at and (_now() - cached_at) < timedelta(hours=6) and _BANK_CACHE["banks"]:
        return _BANK_CACHE["banks"]
    try:
        raw = await PaymentProviderClient(PaymentProvider.PAYSTACK).list_banks()
    except Exception as exc:
        raise HTTPException(502, "Could not load the bank list. Please try again in a moment.")
    banks = sorted(({"name": b["name"], "code": b["code"]} for b in raw if b.get("name") and b.get("code")), key=lambda b: b["name"])
    _BANK_CACHE.update(at=_now(), banks=banks)
    return banks


async def _verified_recipient(account_number: str, bank_code: str) -> tuple[str, str]:
    """Ask the bank for the account holder's name and register a payout recipient. Returns (name, recipient_code)."""
    client = PaymentProviderClient(PaymentProvider.PAYSTACK)
    try:
        account_name = await client.resolve_account(account_number=account_number, bank_code=bank_code)
        response = await client.create_recipient(name=account_name, account_number=account_number, bank_code=bank_code, currency="NGN")
    except Exception as exc:
        raise HTTPException(502, "Could not verify this bank account. Check the account number and bank, then try again.")
    recipient_code = _data(response).get("recipient_code")
    if not recipient_code:
        raise HTTPException(502, "Could not verify this bank account. Please try again.")
    return account_name, recipient_code


async def _bank_name(paystack_code: str) -> str | None:
    try:
        return next((b["name"] for b in await list_banks() if b["code"] == paystack_code), None)
    except HTTPException:
        return None


async def _squad_check(account_number: str, paystack_code: str, bank_name: str | None, account_name: str) -> tuple[str | None, str]:
    """Find the Squad bank code for this account and PROVE it: Squad's account lookup must return the same holder name.
    Returns (code, note). The code is None (so Squad payouts are not used for this account) if Squad is not set up or
    nothing checks out; the note says why, in plain words."""
    if not settings.SQUAD_SECRET_KEY:
        return None, "Squad is not set up (SQUAD_SECRET_KEY is empty), so payouts for this account go through Paystack."
    codes = bank_codes.candidates(paystack_code, bank_name)
    if not codes:
        return None, "No matching Squad bank code was found for this bank, so payouts for this account go through Paystack."
    client = PaymentProviderClient(PaymentProvider.SQUAD)
    last_problem = ""
    for code in codes:
        try:
            squad_name = await client.resolve_account(account_number=account_number, bank_code=code)
        except Exception as exc:
            last_problem = f"Squad's account lookup failed for bank code {code}: {str(exc)[:300]}"
            continue
        if bank_codes.same_person(squad_name, account_name):
            return code, f"Squad confirmed this account with bank code {code}."
        last_problem = f"Squad's lookup for bank code {code} returned a different account name."
    return None, f"{last_problem} Payouts for this account go through Paystack."


async def _squad_code_for(account_number: str, paystack_code: str, bank_name: str | None, account_name: str) -> str | None:
    return (await _squad_check(account_number, paystack_code, bank_name, account_name))[0]


async def recheck_squad(db: AsyncSession, seller: Seller, account_id: uuid.UUID) -> SellerBankAccount:
    """Ask Squad again about a saved account (for example after Squad enabled payouts for the merchant)."""
    account = (await db.execute(
        select(SellerBankAccount).where(SellerBankAccount.id == account_id, SellerBankAccount.seller_id == seller.id)
    )).scalar_one_or_none()
    if not account:
        raise HTTPException(404, "Bank account not found")
    code, note = await _squad_check(decrypt(account.account_number_encrypted), account.bank_code, account.bank_name, account.account_name)
    account.squad_bank_code = code
    await db.commit()
    await db.refresh(account)
    account.squad_check = note  # shown in this response only; not stored
    return account


async def admin_squad_lookup(bank_code: str, account_number: str) -> dict[str, Any]:
    """Admin diagnostic: call Squad's account lookup exactly as the app does and show what came back."""
    if not settings.SQUAD_SECRET_KEY:
        return {"ok": False, "base_url": settings.SQUAD_BASE_URL, "problem": "SQUAD_SECRET_KEY is empty"}
    client = PaymentProviderClient(PaymentProvider.SQUAD)
    try:
        name = await client.resolve_account(account_number=account_number, bank_code=bank_code)
    except Exception as exc:
        return {"ok": False, "base_url": settings.SQUAD_BASE_URL, "problem": str(exc)[:500]}
    return {"ok": True, "base_url": settings.SQUAD_BASE_URL, "account_name": name}


async def add_bank_account(db: AsyncSession, seller: Seller, payload: schemas.BankAccountCreate) -> SellerBankAccount:
    if payload.provider != PaymentProvider.PAYSTACK:
        raise HTTPException(400, "Bank payouts are only available through Paystack")
    account_hash = hash_value(payload.account_number)
    duplicate = (await db.execute(
        select(SellerBankAccount.id).where(SellerBankAccount.seller_id == seller.id, SellerBankAccount.account_number_hash == account_hash)
    )).first()
    if duplicate:
        raise HTTPException(409, "This bank account is already saved")
    account_name, recipient_code = await _verified_recipient(payload.account_number, payload.bank_code)
    bank_name = payload.bank_name or await _bank_name(payload.bank_code)
    squad_code, squad_note = await _squad_check(payload.account_number, payload.bank_code, bank_name, account_name)
    if payload.is_default:
        await db.execute(update(SellerBankAccount).where(SellerBankAccount.seller_id == seller.id).values(is_default=False))
    account = SellerBankAccount(
        seller_id=seller.id, provider=payload.provider, bank_code=payload.bank_code, bank_name=bank_name,
        squad_bank_code=squad_code,
        account_number_encrypted=encrypt(payload.account_number), account_number_hash=account_hash,
        account_number_last4=payload.account_number[-4:], account_name=account_name,
        provider_recipient_code=recipient_code, status=BankAccountStatus.VERIFIED, is_default=payload.is_default,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    account.squad_check = squad_note  # shown in this response only; not stored
    return account


async def list_bank_accounts(db: AsyncSession, seller_id: uuid.UUID):
    return (await db.execute(
        select(SellerBankAccount).where(SellerBankAccount.seller_id == seller_id)
        .order_by(SellerBankAccount.is_default.desc(), SellerBankAccount.created_at.desc())
    )).scalars().all()


def _payout_reference(provider: PaymentProvider) -> str:
    if provider == PaymentProvider.SQUAD:
        # Squad insists every transfer reference starts with the merchant ID
        if not settings.SQUAD_MERCHANT_ID:
            raise ProviderError("SQUAD_MERCHANT_ID is not set")
        return f"{settings.SQUAD_MERCHANT_ID}_{uuid.uuid4().hex[:24]}"
    return _ref("hmpayout", lower=True)


async def _reserve_payout(
    db: AsyncSession, wallet: SellerWallet, account: SellerBankAccount, amount: int, provider: PaymentProvider,
    allocation: PaymentAllocation | None = None,
) -> Payout:
    """Move `amount` from the wallet's available balance to locked and create the payout. Does not commit."""
    fee = 0  # transfer fees are paid by Hamads, not taken from the seller
    payout = Payout(
        seller_id=wallet.seller_id, wallet_id=wallet.id, bank_account_id=account.id, reference=_payout_reference(provider),
        provider=provider, requested_amount_minor=amount, fee_minor=fee, net_amount_minor=amount - fee, currency=wallet.currency,
        status=PayoutStatus.PROCESSING, processing_at=_now(),
        allocation_id=allocation.id if allocation else None,
        seller_order_id=allocation.seller_order_id if allocation else None,
        food_order_id=allocation.food_order_id if allocation else None,
    )
    db.add(payout)
    await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
    wallet.available_minor -= amount
    wallet.locked_minor += amount
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.PAYOUT_RESERVATION, direction="debit", amount=amount,
        reference=payout.reference, description="Payout funds reserved", payout_id=payout.id, affect_lifetime=False,
    )
    return payout


async def create_payout(db: AsyncSession, seller: Seller, payload: schemas.PayoutCreate) -> Payout:
    """Manual withdrawal (SELLER_PAYOUT_MODE=manual): the seller chooses how much of the available balance to send."""
    wallet = await get_or_create_wallet(db, seller.id, lock=True)
    account = (await db.execute(select(SellerBankAccount).where(
        SellerBankAccount.id == payload.bank_account_id, SellerBankAccount.seller_id == seller.id,
        SellerBankAccount.status == BankAccountStatus.VERIFIED,
    ))).scalar_one_or_none()
    if not account:
        raise HTTPException(404, "Verified bank account not found")
    if not account.provider_recipient_code:
        raise HTTPException(409, "This bank account is not set up for payouts. Add it again.")
    if payload.amount_minor > wallet.available_minor:
        raise HTTPException(400, "Insufficient available balance")
    payout = await _reserve_payout(db, wallet, account, payload.amount_minor, PaymentProvider.PAYSTACK)
    await db.commit()  # the money is now reserved; the transfer is attempted next
    return await _send_seller_payout(db, payout, account)


async def _send_seller_payout(db: AsyncSession, payout: Payout, account: SellerBankAccount) -> Payout:
    """Call the provider for a payout whose money is already reserved, and record what happened.

    Rules: a clear "no" from the provider returns the money to the seller's balance. If we cannot tell
    (timeout, network, 5xx) the money stays reserved and the payout stays "processing" until the provider's
    webhook (Paystack) or our requery job (Squad) settles it. A transfer is never repeated with the same reference.
    """
    net = payout.net_amount_minor
    try:
        client = PaymentProviderClient(payout.provider)
        if payout.provider == PaymentProvider.SQUAD:
            response = await client.squad_transfer(
                amount_minor=net, bank_code=account.squad_bank_code, account_number=decrypt(account.account_number_encrypted),
                account_name=account.account_name, reference=payout.reference, remark="Hamads seller payout",
            )
        else:
            response = await client.transfer(
                amount_minor=net, recipient_code=account.provider_recipient_code, reason="Hamads seller payout", reference=payout.reference,
            )
    except ProviderError as exc:
        payout.provider_payload = str(exc)[:2000]
        outcome = _transfer_error_outcome(exc)
        if outcome is None:
            payout.failure_reason = "Waiting for the provider to confirm. This will update by itself."
        else:
            wallet = await get_or_create_wallet(db, payout.seller_id, payout.currency, lock=True)
            await _fail_seller_payout(db, payout, wallet, "The payment provider did not accept this transfer. The money stays in your balance.", outcome)
        await db.commit()
        await db.refresh(payout)
        return payout
    except Exception as exc:
        # we do not know whether the transfer happened (timeout, network): do NOT return the money
        payout.provider_payload = str(exc)[:2000]
        payout.failure_reason = "Waiting for the provider to confirm. This will update by itself."
        await db.commit()
        await db.refresh(payout)
        return payout

    data = _data(response)
    payout.provider_payload = json.dumps(response)
    if payout.provider == PaymentProvider.SQUAD:
        session_id = data.get("nip_transaction_reference")
        payout.provider_reference = str(session_id or payout.reference)
        # Squad's own advice: do not trust the 200 alone; a real transfer carries a bank session ID.
        succeeded = bool(session_id)
    else:
        payout.provider_reference = data.get("transfer_code") or data.get("reference") or payout.reference
        raw = str(data.get("status") or "").lower()
        succeeded = raw in SUCCESS_STATUSES
        if raw == "otp":
            payout.failure_reason = "Paystack is waiting for an OTP. Turn off OTP for transfers in the Paystack dashboard."
    if succeeded:
        wallet = await get_or_create_wallet(db, payout.seller_id, payout.currency, lock=True)
        await _succeed_seller_payout(db, payout, wallet)
    elif payout.provider == PaymentProvider.SQUAD:
        payout.failure_reason = "Waiting for Squad to confirm. This will update by itself."
    await db.commit()
    await db.refresh(payout)
    return payout


def _transfer_error_outcome(exc: ProviderError) -> PayoutStatus | None:
    """What a provider error means for the money. FAILED or REVERSED: nothing was sent, give it back.
    None: we cannot tell (timeout, provider error), so keep it reserved and let the webhook or the requery job decide."""
    code = exc.status_code
    if code is None:
        # no answer from the provider: either our own setup is incomplete (nothing was sent) or the call was lost
        text = str(exc)
        setup_problem = any(word in text for word in ("is not configured", "is not set", "only supported", "only for"))
        return PayoutStatus.FAILED if setup_problem else None
    if code == 412:
        return PayoutStatus.REVERSED
    if code == 424 or code >= 500:
        return None
    return PayoutStatus.FAILED


async def _succeed_seller_payout(db: AsyncSession, payout: Payout, wallet: SellerWallet) -> None:
    await AccountingBridge.on_payout_succeeded(db, payout)
    payout.status = PayoutStatus.SUCCESSFUL
    payout.completed_at = _now()
    payout.failure_reason = None
    wallet.locked_minor -= payout.requested_amount_minor
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.PAYOUT_SUCCESS, direction="debit", amount=payout.requested_amount_minor,
        reference=payout.reference + "-success", description="Seller payout completed", payout_id=payout.id,
    )
    await notifications.notify(
        db, "seller", payout.seller_id, ev.PAYOUT_SENT, "Payout sent",
        f"{_money(payout.requested_amount_minor)} was sent to your bank account.",
        {"payout_id": str(payout.id), "seller_order_id": str(payout.seller_order_id) if payout.seller_order_id else None,
         "food_order_id": str(payout.food_order_id) if payout.food_order_id else None},
    )


async def _fail_seller_payout(db: AsyncSession, payout: Payout, wallet: SellerWallet, reason: str, status: PayoutStatus) -> None:
    payout.status = status
    payout.failure_reason = reason
    payout.failed_at = _now()
    wallet.locked_minor -= payout.requested_amount_minor
    wallet.available_minor += payout.requested_amount_minor
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.PAYOUT_FAILED_REVERSAL, direction="credit", amount=payout.requested_amount_minor,
        reference=payout.reference + "-reversal", description="Failed payout funds returned", payout_id=payout.id,
    )
    earlier_failures = 0
    if payout.allocation_id is not None:  # automatic payouts retry: only tell the seller about the first failure
        earlier_failures = (await db.execute(
            select(func.count(Payout.id)).where(
                Payout.allocation_id == payout.allocation_id, Payout.id != payout.id,
                Payout.status.in_((PayoutStatus.FAILED, PayoutStatus.REVERSED)),
            )
        )).scalar_one()
    if not earlier_failures:
        retry = " We will try again automatically." if payout.allocation_id is not None else ""
        await notifications.notify(
            db, "seller", payout.seller_id, ev.PAYOUT_FAILED, "Payout could not be sent",
            f"A payout of {_money(payout.requested_amount_minor)} did not go through. The money is back in your balance.{retry}",
            {"payout_id": str(payout.id)},
        )


async def list_payouts(db: AsyncSession, seller_id: uuid.UUID):
    return (await db.execute(select(Payout).where(Payout.seller_id == seller_id).order_by(Payout.created_at.desc()))).scalars().all()


async def settle_transfer_event(db: AsyncSession, reference: str, outcome: str) -> bool:
    """A transfer webhook arrived (outcome: success / failed / reversed). Finishes a seller or rider payout."""
    ref = reference.lower()
    payout = (await db.execute(select(Payout).where(func.lower(Payout.reference) == ref).with_for_update())).scalar_one_or_none()
    if payout:
        wallet = await get_or_create_wallet(db, payout.seller_id, payout.currency, lock=True)
        if payout.status in (PayoutStatus.PROCESSING, PayoutStatus.REQUESTED):
            if outcome == "success":
                await _succeed_seller_payout(db, payout, wallet)
            else:
                await _fail_seller_payout(db, payout, wallet, f"Transfer {outcome} at the provider",
                                          PayoutStatus.REVERSED if outcome == "reversed" else PayoutStatus.FAILED)
        elif payout.status == PayoutStatus.SUCCESSFUL and outcome == "reversed":
            # the bank bounced it after we marked it paid: return the money to the seller
            payout.status = PayoutStatus.REVERSED
            payout.failure_reason = "Transfer reversed by the provider after it was marked paid"
            wallet.available_minor += payout.requested_amount_minor
            await _wallet_entry(
                db, wallet, entry_type=WalletEntryType.PAYOUT_FAILED_REVERSAL, direction="credit", amount=payout.requested_amount_minor,
                reference=payout.reference + "-reversal", description="Reversed payout funds returned", payout_id=payout.id,
            )
            await notifications.notify(
                db, "seller", payout.seller_id, ev.PAYOUT_FAILED, "Payout was returned",
                f"The bank sent back a payout of {_money(payout.requested_amount_minor)}. The money is back in your balance.",
                {"payout_id": str(payout.id)},
            )
        await db.commit()
        return True

    rider_payout = (await db.execute(select(RiderPayout).where(func.lower(RiderPayout.reference) == ref).with_for_update())).scalar_one_or_none()
    if rider_payout:
        if rider_payout.status in (PayoutStatus.PROCESSING, PayoutStatus.REQUESTED):
            if outcome == "success":
                rider_payout.status = PayoutStatus.SUCCESSFUL
                rider_payout.completed_at = _now()
                rider_payout.failure_reason = None
                await _tell_rider_paid(db, rider_payout)
            else:
                await _fail_rider_payout(db, rider_payout, f"Transfer {outcome} at the provider",
                                         PayoutStatus.REVERSED if outcome == "reversed" else PayoutStatus.FAILED)
        await db.commit()
        return True
    return False


# ---------------------------------------------------------------------------
# automatic payouts: one per order, sent when its hold is over
# ---------------------------------------------------------------------------
#
# Flow (SELLER_PAYOUT_MODE=auto): the order is delivered/completed -> 2 hours later release_eligible_allocations()
# marks the money "released" -> process_auto_payouts() sends that order's money to the seller's bank through the
# provider the customer paid with. Transfer fees are paid by Hamads. If the seller has no bank account, or the
# provider balance is too low, the money simply waits and the next run tries again.

async def _default_bank_account(db: AsyncSession, seller_id: uuid.UUID) -> SellerBankAccount | None:
    return (await db.execute(
        select(SellerBankAccount).where(SellerBankAccount.seller_id == seller_id, SellerBankAccount.status == BankAccountStatus.VERIFIED)
        .order_by(SellerBankAccount.is_default.desc(), SellerBankAccount.created_at.desc()).limit(1)
    )).scalars().first()


def _payout_provider(paid_with: PaymentProvider, account: SellerBankAccount) -> PaymentProvider | None:
    """Pay out through the provider the customer paid with, when this account can use it; otherwise through Paystack."""
    if paid_with == PaymentProvider.SQUAD and account.squad_bank_code and settings.SQUAD_SECRET_KEY and settings.SQUAD_MERCHANT_ID:
        return PaymentProvider.SQUAD
    if account.provider_recipient_code:
        return PaymentProvider.PAYSTACK
    return None


async def _open_discrepancy(db: AsyncSession, provider: PaymentProvider, reference: str, kind: str, details: str, payment_id=None) -> bool:
    """Record something for an admin, once. Returns True if it was newly recorded. Does not commit."""
    already = (await db.execute(select(PaymentDiscrepancy.id).where(
        PaymentDiscrepancy.reference == reference, PaymentDiscrepancy.discrepancy_type == kind,
        PaymentDiscrepancy.status == PaymentDiscrepancyStatus.OPEN,
    ).limit(1))).first()
    if already:
        return False
    db.add(PaymentDiscrepancy(payment_id=payment_id, provider=provider, reference=reference, discrepancy_type=kind, details=details))
    await notifications.notify_admins(
        db, ev.NEEDS_ATTENTION, "A payout needs a look", details[:300], {"kind": kind, "reference": reference},
    )
    return True


async def process_auto_payouts(db: AsyncSession, limit: int = 100) -> dict[str, int]:
    """Send the money of every released order that has not been paid out yet. Safe to run often and in parallel."""
    counts: dict[str, int] = {}
    live = exists().where(Payout.allocation_id == PaymentAllocation.id, Payout.status.in_(LIVE_PAYOUT_STATES))
    allocation_ids = (await db.execute(
        select(PaymentAllocation.id)
        .where(PaymentAllocation.status == AllocationStatus.RELEASED, PaymentAllocation.seller_net_minor > 0, ~live)
        .order_by(PaymentAllocation.released_at).limit(limit)
    )).scalars().all()
    await db.rollback()
    balances: dict[PaymentProvider, int | None] = {}
    for allocation_id in allocation_ids:
        try:
            outcome = await _auto_payout_one(db, allocation_id, balances)
        except Exception:
            log.exception("automatic payout failed for allocation %s", allocation_id)
            await db.rollback()
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


async def _auto_payout_one(db: AsyncSession, allocation_id: uuid.UUID, balances: dict[PaymentProvider, int | None]) -> str:
    allocation = (await db.execute(
        select(PaymentAllocation).where(PaymentAllocation.id == allocation_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if allocation is None or allocation.status != AllocationStatus.RELEASED or allocation.seller_net_minor <= 0:
        await db.rollback()
        return "skipped"  # another run has it, or it changed since we looked
    if (await db.execute(select(Payout.id).where(Payout.allocation_id == allocation.id, Payout.status.in_(LIVE_PAYOUT_STATES)).limit(1))).first():
        await db.rollback()
        return "skipped"

    failed_count, last_failed = (await db.execute(
        select(func.count(Payout.id), func.max(Payout.failed_at))
        .where(Payout.allocation_id == allocation.id, Payout.status.in_((PayoutStatus.FAILED, PayoutStatus.REVERSED)))
    )).one()
    payment = await db.get(Payment, allocation.payment_id)
    if failed_count >= settings.PAYOUT_MAX_ATTEMPTS:
        await _open_discrepancy(
            db, payment.provider, payment.transaction_ref, "payout_not_sent",
            f"The automatic payout for this order failed {failed_count} times and was stopped. Check the provider balance and the seller's bank account.",
            payment_id=payment.id,
        )
        await db.commit()
        return "flagged"
    if last_failed and (_now() - last_failed) < timedelta(minutes=settings.PAYOUT_RETRY_MINUTES):
        await db.rollback()
        return "waiting_to_retry"

    account = await _default_bank_account(db, allocation.seller_id)
    provider = _payout_provider(payment.provider, account) if account else None
    if account is None or provider is None:
        if account is None and not await notifications.recently_notified(db, "seller", allocation.seller_id, ev.BANK_ACCOUNT_NEEDED):
            await notifications.notify(
                db, "seller", allocation.seller_id, ev.BANK_ACCOUNT_NEEDED, "Add your bank account",
                f"We have {_money(allocation.seller_net_minor)} ready to pay you but no bank account on file. Add one and we will send it automatically.",
                {"screen": "bank_accounts"},
            )
            await db.commit()
        else:
            await db.rollback()
        return "waiting_for_bank_account"

    amount = allocation.seller_net_minor
    if provider not in balances:
        balances[provider] = await PaymentProviderClient(provider).balance()  # None when unknown: then just try
    balance = balances[provider]
    if balance is not None and balance < amount:
        await db.rollback()
        return "waiting_for_funds"

    wallet = await get_or_create_wallet(db, allocation.seller_id, lock=True)
    if wallet.available_minor < amount:
        await db.rollback()
        return "wallet_short"  # earlier refunds took money back: wait until later sales cover it
    try:
        payout = await _reserve_payout(db, wallet, account, amount, provider, allocation)
        await db.commit()
    except IntegrityError:  # another run created this order's payout at the same moment
        await db.rollback()
        return "skipped"
    if balance is not None:
        balances[provider] = balance - amount
    await _send_seller_payout(db, payout, account)
    return "sent"


def _requery_outcome(response: dict[str, Any]) -> str | None:
    """Read Squad's answer to "what happened to this transfer?" as success / failed / reversed, or None if still pending."""
    data = _data(response)
    raw = str(data.get("transaction_status") or data.get("status") or data.get("payout_status") or "").lower()
    if raw in SUCCESS_STATUSES:
        return "success"
    if raw == "reversed":
        return "reversed"
    if raw in FAILED_STATUSES:
        return "failed"
    return None


async def refresh_squad_payouts(db: AsyncSession, limit: int = 100) -> int:
    """Squad has no payout webhook in its docs, so ask it about every transfer that is still processing."""
    now = _now()
    rows = (await db.execute(
        select(Payout.reference, Payout.processing_at)
        .where(Payout.status == PayoutStatus.PROCESSING, Payout.provider == PaymentProvider.SQUAD,
               Payout.processing_at <= now - timedelta(minutes=2))
        .order_by(Payout.processing_at).limit(limit)
    )).all()
    await db.rollback()
    changed = 0
    for reference, processing_at in rows:
        try:
            response = await PaymentProviderClient(PaymentProvider.SQUAD).squad_requery(reference)
        except ProviderError as exc:
            # Squad never heard of it. After a grace period that means no money moved: give it back.
            if exc.status_code == 404 and (now - processing_at) > timedelta(minutes=15):
                changed += 1 if await settle_transfer_event(db, reference, "failed") else 0
            continue
        except Exception:
            continue
        outcome = _requery_outcome(response)
        if outcome and await settle_transfer_event(db, reference, outcome):
            changed += 1
    return changed


async def flag_stuck_payouts(db: AsyncSession, hours: int = 24) -> int:
    """Show an admin any payout that has been "processing" for a day: someone has to check with the provider."""
    cutoff = _now() - timedelta(hours=hours)
    stuck = (await db.execute(
        select(Payout).where(Payout.status == PayoutStatus.PROCESSING, Payout.processing_at <= cutoff).limit(100)
    )).scalars().all()
    flagged = 0
    for payout in stuck:
        if await _open_discrepancy(
            db, payout.provider, payout.reference, "payout_stuck",
            f"A payout of {payout.requested_amount_minor} kobo has been processing for over {hours} hours. Check it with {payout.provider.value}.",
        ):
            flagged += 1
    await db.commit()
    return flagged


async def run_seller_payout_cycle(db: AsyncSession) -> dict[str, Any]:
    """The whole seller-money cycle, in order: release finished holds, send payouts, settle Squad transfers, flag the stuck.
    Run it every 10 minutes (Celery beat, or POST /admin/payments/seller-payouts/run from an outside scheduler)."""
    result: dict[str, Any] = {"released": await release_eligible_allocations(db)}
    if settings.SELLER_PAYOUT_MODE == "auto":
        result["payouts"] = await process_auto_payouts(db)
    result["squad_updated"] = await refresh_squad_payouts(db)
    result["stuck_flagged"] = await flag_stuck_payouts(db)
    return result


# ---------------------------------------------------------------------------
# refunds
# ---------------------------------------------------------------------------

async def _open_refund(
    db: AsyncSession, payment: Payment, reason: str, *, amount: int, order_id=None, food_order_id=None, seller_order_id=None,
    tell_admins: bool = True,
) -> Refund:
    """Create a refund request for an admin to approve (and tell the admins). Does not commit."""
    refund = Refund(
        payment_id=payment.id, order_id=order_id, food_order_id=food_order_id, seller_order_id=seller_order_id,
        requested_by=payment.customer_id, reference=_ref("HMREFUND"), amount_minor=amount, currency=payment.currency,
        reason=reason[:500], status=RefundStatus.REQUESTED,
    )
    db.add(refund)
    if tell_admins:
        await notifications.notify_admins(
            db, ev.REFUND_REQUESTED, "A refund needs approval", f"{_money(amount)} to refund. Reason: {reason[:200]}",
            {"payment_id": str(payment.id), "reference": refund.reference},
        )
    return refund


async def _open_refund_total(db: AsyncSession, payment_id: uuid.UUID, seller_order_id: uuid.UUID | None = None) -> int:
    stmt = select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(Refund.payment_id == payment_id, Refund.status.in_(OPEN_REFUND_STATES))
    if seller_order_id is not None:
        stmt = stmt.where(Refund.seller_order_id == seller_order_id)
    return int((await db.execute(stmt)).scalar_one())


async def _successful_refund_total(db: AsyncSession, payment_id: uuid.UUID) -> int:
    return int((await db.execute(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(Refund.payment_id == payment_id, Refund.status == RefundStatus.SUCCESSFUL)
    )).scalar_one())


async def on_seller_order_cancelled(db: AsyncSession, so: SellerOrder) -> None:
    """A seller cancelled a paid seller order: open a refund for that seller's part. Caller commits."""
    allocation = (await db.execute(
        select(PaymentAllocation).where(PaymentAllocation.seller_order_id == so.id).with_for_update()
    )).scalar_one_or_none()
    if not allocation or allocation.status in (AllocationStatus.REFUNDED, AllocationStatus.CANCELLED):
        return
    open_total = await _open_refund_total(db, allocation.payment_id, so.id)
    amount = allocation.gross_amount_minor - allocation.refunded_gross_minor - open_total
    if amount <= 0:
        return
    # If this was the last live part of the order, the customer gets the delivery fee back too.
    order = await db.get(Order, so.order_id)
    if order is not None and order.delivery_fee and order.delivery_fee > 0:
        await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
        still_live = (await db.execute(
            select(func.count(SellerOrder.id)).where(
                SellerOrder.order_id == so.order_id, SellerOrder.id != so.id, SellerOrder.status != SellerOrderStatus.CANCELLED,
            )
        )).scalar_one()
        if still_live == 0:
            amount += naira_to_minor(order.delivery_fee)
    payment = await db.get(Payment, allocation.payment_id)
    await _open_refund(db, payment, f"Seller cancelled the order: {so.cancellation_reason or 'no reason given'}",
                 amount=amount, order_id=so.order_id, seller_order_id=so.id)


async def on_food_order_cancelled(db: AsyncSession, order: FoodOrder) -> None:
    """A paid food order was cancelled or rejected: open a full refund for an admin to approve. Caller commits."""
    if not order.refund_required:
        return
    payment = (await db.execute(
        select(Payment).where(Payment.food_order_id == order.id, Payment.status == PaymentStatus.SUCCESSFUL)
        .order_by(Payment.paid_at.desc()).limit(1)
    )).scalars().first()
    if not payment:
        return
    if await _open_refund_total(db, payment.id) + await _successful_refund_total(db, payment.id) > 0:
        return
    await _open_refund(db, payment, order.cancellation_reason or "Food order cancelled",
                 amount=payment.amount_minor, food_order_id=order.id)


async def request_refund(db: AsyncSession, user: User, payload: schemas.RefundCreate) -> Refund:
    payment = (await db.execute(
        select(Payment).where(Payment.id == payload.payment_id, Payment.customer_id == user.id).with_for_update()
    )).scalar_one_or_none()
    if not payment or payment.order_id is None or payment.status not in (PaymentStatus.SUCCESSFUL, PaymentStatus.PARTIALLY_REFUNDED):
        raise HTTPException(400, "Payment is not refundable")
    allocation = (await db.execute(select(PaymentAllocation).where(
        PaymentAllocation.payment_id == payment.id, PaymentAllocation.seller_order_id == payload.seller_order_id,
    ))).scalar_one_or_none()
    if not allocation:
        raise HTTPException(400, "That seller order is not part of this payment")
    available = allocation.gross_amount_minor - allocation.refunded_gross_minor - await _open_refund_total(db, payment.id, payload.seller_order_id)
    if payload.amount_minor > available:
        raise HTTPException(400, f"You can ask for at most {available} kobo back for this seller order")
    refund = await _open_refund(db, payment, payload.reason, amount=payload.amount_minor, order_id=payment.order_id, seller_order_id=payload.seller_order_id)
    await db.commit()
    await db.refresh(refund)
    return refund


async def admin_refund_payment(db: AsyncSession, admin_id: uuid.UUID, payment_id: uuid.UUID, payload: schemas.AdminRefundRequest) -> Refund:
    """An admin refunds a payment straight away (full by default)."""
    payment = (await db.execute(select(Payment).where(Payment.id == payment_id).with_for_update())).scalar_one_or_none()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status not in (PaymentStatus.SUCCESSFUL, PaymentStatus.PARTIALLY_REFUNDED):
        raise HTTPException(400, "Payment is not refundable")
    remaining = payment.amount_minor - await _successful_refund_total(db, payment.id) - await _open_refund_total(db, payment.id)
    amount = payload.amount_minor or remaining
    if amount <= 0 or amount > remaining:
        raise HTTPException(400, f"At most {remaining} kobo can still be refunded on this payment")
    full = amount == payment.amount_minor and remaining == payment.amount_minor
    if payment.food_order_id is not None:
        if not full:
            raise HTTPException(400, "Food orders can only be refunded in full")
    elif payload.seller_order_id is not None:
        allocation = (await db.execute(select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment.id, PaymentAllocation.seller_order_id == payload.seller_order_id,
        ))).scalar_one_or_none()
        if not allocation:
            raise HTTPException(400, "That seller order is not part of this payment")
    elif not full:
        raise HTTPException(400, "A partial refund on a marketplace order needs a seller_order_id")
    refund = await _open_refund(
        db, payment, payload.reason, amount=amount, order_id=payment.order_id, food_order_id=payment.food_order_id,
        seller_order_id=payload.seller_order_id, tell_admins=False,  # an admin is doing this right now
    )
    await db.commit()
    return await process_refund(db, refund.id, admin_id, approve=True)


async def process_refund(db: AsyncSession, refund_id: uuid.UUID, admin_id: uuid.UUID, approve: bool = True) -> Refund:
    refund = (await db.execute(select(Refund).where(Refund.id == refund_id).with_for_update())).scalar_one_or_none()
    if not refund:
        raise HTTPException(404, "Refund not found")
    if refund.status not in (RefundStatus.REQUESTED, RefundStatus.APPROVED):
        raise HTTPException(409, "Refund cannot be processed in its current state")
    refund.reviewed_by = admin_id
    if not approve:
        refund.status = RefundStatus.REJECTED
        await _tell_customer_refund(db, refund, approved=False)
        await db.commit()
        await db.refresh(refund)
        return refund
    payment = (await db.execute(select(Payment).where(Payment.id == refund.payment_id))).scalar_one()
    if payment.status not in (PaymentStatus.SUCCESSFUL, PaymentStatus.PARTIALLY_REFUNDED):
        raise HTTPException(409, "The payment is not in a refundable state")
    refund.status = RefundStatus.PROCESSING
    await db.commit()  # marks it as in progress so nobody processes it twice

    try:
        response = await PaymentProviderClient(payment.provider).refund(
            payment.transaction_ref, refund.amount_minor if refund.amount_minor < payment.amount_minor else None,
            refund.reason, gateway_ref=payment.provider_ref,
        )
    except Exception as exc:
        refund.status = RefundStatus.FAILED
        refund.admin_note = str(exc)[:500]
        await db.commit()
        await db.refresh(refund)
        return refund

    data = _data(response)
    payment = (await db.execute(select(Payment).where(Payment.id == refund.payment_id).with_for_update())).scalar_one()
    refund.provider_refund_reference = str(data.get("id") or data.get("refund_reference") or data.get("reference") or refund.reference)
    refund.status = RefundStatus.SUCCESSFUL  # accepted by the provider; the money reaches the customer in a few days
    refund.processed_at = _now()
    await _apply_refund_to_wallets(db, refund, payment)
    total = await _successful_refund_total(db, payment.id)
    payment.status = PaymentStatus.REFUNDED if total >= payment.amount_minor else PaymentStatus.PARTIALLY_REFUNDED
    if refund.food_order_id is not None:
        fo = await db.get(FoodOrder, refund.food_order_id)
        if fo and fo.status == FoodOrderStatus.CANCELLED:
            fo.refund_required = False
    await _tell_customer_refund(db, refund, approved=True)
    await db.commit()
    await db.refresh(refund)
    return refund


async def _tell_customer_refund(db: AsyncSession, refund: Refund, approved: bool) -> None:
    if refund.requested_by is None:
        return
    data = {"refund_id": str(refund.id), "order_id": str(refund.order_id) if refund.order_id else None,
            "food_order_id": str(refund.food_order_id) if refund.food_order_id else None}
    if approved:
        await notifications.notify(
            db, "customer", refund.requested_by, ev.REFUND_APPROVED, "Refund approved",
            f"Your refund of {_money(refund.amount_minor)} was approved. It reaches your account in a few days.", data,
        )
    else:
        await notifications.notify(
            db, "customer", refund.requested_by, ev.REFUND_REJECTED, "Refund not approved",
            f"Your refund request of {_money(refund.amount_minor)} was not approved.", data,
        )


async def _apply_refund_to_wallets(db: AsyncSession, refund: Refund, payment: Payment) -> None:
    """Take the refunded part back from the seller(s) it was credited to."""
    stmt = select(PaymentAllocation).where(PaymentAllocation.payment_id == payment.id).with_for_update()
    targeted = True
    if refund.seller_order_id is not None:
        stmt = stmt.where(PaymentAllocation.seller_order_id == refund.seller_order_id)
    elif refund.food_order_id is not None:
        stmt = stmt.where(PaymentAllocation.food_order_id == refund.food_order_id)
    elif refund.amount_minor == payment.amount_minor:
        targeted = False  # a full refund of a whole marketplace payment: every seller gives their part back
    else:
        return
    for allocation in (await db.execute(stmt)).scalars().all():
        remaining_gross = allocation.gross_amount_minor - allocation.refunded_gross_minor
        gross_part = min(refund.amount_minor, remaining_gross) if targeted else remaining_gross
        await _reverse_allocation(db, allocation, gross_part, refund)


async def _reverse_allocation(db: AsyncSession, allocation: PaymentAllocation, gross_part: int, refund: Refund) -> None:
    remaining_gross = allocation.gross_amount_minor - allocation.refunded_gross_minor
    gross_part = min(gross_part, remaining_gross)
    if gross_part <= 0:
        return
    if gross_part == remaining_gross:
        deduction = allocation.seller_net_minor
    else:
        deduction = (gross_part * allocation.seller_net_minor) // remaining_gross
    wallet = await get_or_create_wallet(db, allocation.seller_id, lock=True)
    if allocation.status == AllocationStatus.RELEASED:
        wallet.available_minor -= deduction  # may go negative: no withdrawals until later sales cover it
    else:
        from_pending = min(wallet.pending_minor, deduction)
        wallet.pending_minor -= from_pending
        wallet.available_minor -= deduction - from_pending
    allocation.seller_net_minor -= deduction
    allocation.refunded_gross_minor += gross_part
    if allocation.refunded_gross_minor >= allocation.gross_amount_minor:
        allocation.status = AllocationStatus.REFUNDED
    await _wallet_entry(
        db, wallet, entry_type=WalletEntryType.REFUND, direction="debit", amount=deduction, reference=refund.reference,
        description="Refunded to the customer", refund_id=refund.id, allocation_id=allocation.id, order_id=allocation.order_id,
        seller_order_id=allocation.seller_order_id, food_order_id=allocation.food_order_id,
    )


# ---------------------------------------------------------------------------
# riders: bank account, earnings, daily payout
# ---------------------------------------------------------------------------

async def add_rider_bank_account(db: AsyncSession, rider: Rider, payload: schemas.RiderBankAccountCreate) -> RiderBankAccount:
    account_hash = hash_value(payload.account_number)
    duplicate = (await db.execute(
        select(RiderBankAccount.id).where(RiderBankAccount.rider_id == rider.id, RiderBankAccount.account_number_hash == account_hash)
    )).first()
    if duplicate:
        raise HTTPException(409, "This bank account is already saved")
    account_name, recipient_code = await _verified_recipient(payload.account_number, payload.bank_code)
    await db.execute(update(RiderBankAccount).where(RiderBankAccount.rider_id == rider.id).values(is_default=False))
    account = RiderBankAccount(
        rider_id=rider.id, bank_code=payload.bank_code, bank_name=payload.bank_name,
        account_number_encrypted=encrypt(payload.account_number), account_number_hash=account_hash,
        account_number_last4=payload.account_number[-4:], account_name=account_name,
        provider_recipient_code=recipient_code, status=BankAccountStatus.VERIFIED, is_default=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def list_rider_bank_accounts(db: AsyncSession, rider_id: uuid.UUID):
    return (await db.execute(
        select(RiderBankAccount).where(RiderBankAccount.rider_id == rider_id)
        .order_by(RiderBankAccount.is_default.desc(), RiderBankAccount.created_at.desc())
    )).scalars().all()


async def rider_earnings_summary(db: AsyncSession, rider_id: uuid.UUID) -> schemas.RiderEarningsOut:
    unpaid_total, unpaid_count = (await db.execute(
        select(func.coalesce(func.sum(RiderEarning.amount_minor), 0), func.count(RiderEarning.id))
        .where(RiderEarning.rider_id == rider_id, RiderEarning.payout_id.is_(None))
    )).one()
    paid_total = (await db.execute(
        select(func.coalesce(func.sum(RiderPayout.amount_minor), 0))
        .where(RiderPayout.rider_id == rider_id, RiderPayout.status == PayoutStatus.SUCCESSFUL)
    )).scalar_one()
    return schemas.RiderEarningsOut(
        unpaid_minor=int(unpaid_total), unpaid_deliveries=int(unpaid_count), paid_minor=int(paid_total),
        next_payout=f"Paid daily around {(settings.RIDER_PAYOUT_HOUR_UTC + 1) % 24:02d}:00 Nigerian time, once you have a bank account saved",
    )


async def list_rider_payouts(db: AsyncSession, rider_id: uuid.UUID | None = None, status: PayoutStatus | None = None, limit: int = 100):
    stmt = select(RiderPayout).order_by(RiderPayout.created_at.desc()).limit(limit)
    if rider_id is not None:
        stmt = stmt.where(RiderPayout.rider_id == rider_id)
    if status is not None:
        stmt = stmt.where(RiderPayout.status == status)
    return (await db.execute(stmt)).scalars().all()


async def create_daily_rider_payouts(db: AsyncSession, auto_send: bool | None = None) -> list[RiderPayout]:
    """Bundle each rider's unpaid delivery earnings into one payout. Riders with no saved bank account wait.

    With auto_send (default: the RIDER_PAYOUT_AUTO setting) the transfers are sent straight away;
    otherwise the payouts wait as "requested" for an admin to approve.
    """
    cutoff = _now()
    earnings = (await db.execute(
        select(RiderEarning).where(RiderEarning.payout_id.is_(None), RiderEarning.earned_at <= cutoff)
        .order_by(RiderEarning.rider_id).with_for_update(skip_locked=True)
    )).scalars().all()
    by_rider: dict[uuid.UUID, list[RiderEarning]] = {}
    for e in earnings:
        by_rider.setdefault(e.rider_id, []).append(e)

    created: list[RiderPayout] = []
    for rider_id, rows in by_rider.items():
        account = (await db.execute(
            select(RiderBankAccount).where(
                RiderBankAccount.rider_id == rider_id, RiderBankAccount.status == BankAccountStatus.VERIFIED,
                RiderBankAccount.provider_recipient_code.is_not(None),
            ).order_by(RiderBankAccount.is_default.desc(), RiderBankAccount.created_at.desc()).limit(1)
        )).scalars().first()
        if not account:
            continue
        payout = RiderPayout(
            rider_id=rider_id, bank_account_id=account.id, reference=_ref("hmrider", lower=True),
            amount_minor=sum(e.amount_minor for e in rows), deliveries_count=len(rows), status=PayoutStatus.REQUESTED,
        )
        db.add(payout)
        await db.flush()
        await AccountingBridge.on_payment_captured(db, payment)
        for e in rows:
            e.payout_id = payout.id
        created.append(payout)
    await db.commit()

    send_now = settings.RIDER_PAYOUT_AUTO if auto_send is None else auto_send
    if send_now:
        for payout in created:
            try:
                await send_rider_payout(db, payout.id, admin_id=None)
            except HTTPException:
                continue  # leave it "requested" for an admin
    return created


async def send_rider_payout(db: AsyncSession, payout_id: uuid.UUID, admin_id: uuid.UUID | None) -> RiderPayout:
    """Send the transfer for a requested rider payout (admin approval, or the daily job in auto mode)."""
    payout = (await db.execute(select(RiderPayout).where(RiderPayout.id == payout_id).with_for_update())).scalar_one_or_none()
    if not payout:
        raise HTTPException(404, "Payout not found")
    if payout.status != PayoutStatus.REQUESTED:
        raise HTTPException(409, "This payout has already been sent or closed")
    account = await db.get(RiderBankAccount, payout.bank_account_id)
    if not account or account.status != BankAccountStatus.VERIFIED or not account.provider_recipient_code:
        raise HTTPException(409, "The rider's bank account is not ready for payouts")
    payout.status = PayoutStatus.PROCESSING
    payout.approved_by = admin_id
    payout.processing_at = _now()
    await db.commit()  # marked in progress before any money moves

    try:
        response = await PaymentProviderClient(PaymentProvider.PAYSTACK).transfer(
            amount_minor=payout.amount_minor, recipient_code=account.provider_recipient_code,
            reason="Hamads rider daily pay", reference=payout.reference,
        )
    except ProviderError as exc:
        payout.provider_payload = str(exc)[:2000]
        await _fail_rider_payout(db, payout, "The payment provider rejected this transfer.", PayoutStatus.FAILED)
        await db.commit()
        await db.refresh(payout)
        return payout
    except Exception as exc:
        payout.provider_payload = str(exc)[:2000]
        payout.failure_reason = "Waiting for the provider to confirm. This will update by itself."
        await db.commit()
        await db.refresh(payout)
        return payout

    data = _data(response)
    payout.provider_reference = data.get("transfer_code") or data.get("reference") or payout.reference
    payout.provider_payload = json.dumps(response)
    raw = str(data.get("status") or "").lower()
    if raw in SUCCESS_STATUSES:
        payout.status = PayoutStatus.SUCCESSFUL
        payout.completed_at = _now()
        await _tell_rider_paid(db, payout)
    elif raw == "otp":
        payout.failure_reason = "Paystack is waiting for an OTP. Turn off OTP for transfers in the Paystack dashboard."
    await db.commit()
    await db.refresh(payout)
    return payout


async def _tell_rider_paid(db: AsyncSession, payout: RiderPayout) -> None:
    await notifications.notify(
        db, "rider", payout.rider_id, ev.RIDER_PAYOUT_SENT, "You have been paid",
        f"{_money(payout.amount_minor)} for {payout.deliveries_count} deliver{'y' if payout.deliveries_count == 1 else 'ies'} was sent to your bank account.",
        {"payout_id": str(payout.id)},
    )


async def _fail_rider_payout(db: AsyncSession, payout: RiderPayout, reason: str, status: PayoutStatus) -> None:
    """The payout did not go through: free its earnings so tomorrow's run picks them up again."""
    payout.status = status
    payout.failure_reason = reason
    payout.failed_at = _now()
    await db.execute(update(RiderEarning).where(RiderEarning.payout_id == payout.id).values(payout_id=None))
    await notifications.notify(
        db, "rider", payout.rider_id, ev.RIDER_PAYOUT_FAILED, "Your pay could not be sent",
        "Today's payout did not go through. Your earnings are safe and will be included in the next payout.",
        {"payout_id": str(payout.id)},
    )
    await notifications.notify_admins(
        db, ev.NEEDS_ATTENTION, "A rider payout failed", f"{_money(payout.amount_minor)} rider payout failed: {reason[:200]}",
        {"kind": "rider_payout_failed", "payout_id": str(payout.id)},
    )
