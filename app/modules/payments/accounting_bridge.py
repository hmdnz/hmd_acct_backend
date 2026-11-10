"""Accounting Bridge: Synchronizes Payments module events with the Double-Entry Accounting Ledger.

Enforces zero duplication:
Payments handles operational money movement, Squad/Paystack webhooks, and NUBAN bank accounts.
Accounting handles the General Ledger, Balance Sheet, IFRS 15, and Financial Statements.
"""

import logging
from decimal import Decimal
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.service import AccountingService
from app.modules.payments.models import Payment, Payout, Refund
from app.modules.orders.models import SellerOrder

logger = logging.getLogger("hamads.accounting_bridge")


def minor_to_naira(amount_kobo: int) -> Decimal:
    """Converts kobo to Decimal Naira."""
    return Decimal(str(amount_kobo)) / Decimal("100")


class AccountingBridge:
    @staticmethod
    async def on_payment_captured(db: AsyncSession, payment: Payment) -> None:
        """Books double-entry journal entry upon customer payment capture."""
        try:
            acc_service = AccountingService(db)
            gross_naira = minor_to_naira(payment.amount)
            # Default placeholder user/system actor
            actor_id = payment.customer_id
            
            await acc_service.record_order_payment(
                order_id=payment.order_id or payment.id,
                gross_amount=gross_naira,
                currency="NGN",
                gateway_ref=payment.transaction_ref,
                user_id=actor_id
            )
            logger.info("Double-entry journal entry recorded for payment %s", payment.transaction_ref)
        except Exception as e:
            logger.error("Failed to post accounting entry for payment %s: %s", payment.id, e, exc_info=True)

    @staticmethod
    async def on_order_delivered(db: AsyncSession, seller_order: SellerOrder) -> None:
        """Recognizes platform commission and releases customer escrow to seller payable (IFRS 15)."""
        try:
            acc_service = AccountingService(db)
            subtotal = Decimal(str(seller_order.subtotal_price)) if hasattr(seller_order, "subtotal_price") else Decimal("0")
            shipping = Decimal(str(getattr(seller_order, "shipping_fee", 0) or 0))
            actor_id = getattr(seller_order, "seller_id", uuid.uuid4())

            await acc_service.recognize_delivered_order_escrow(
                order_id=seller_order.order_id,
                seller_order_id=seller_order.id,
                vendor_id=seller_order.seller_id,
                item_subtotal=subtotal,
                shipping_fee=shipping,
                user_id=actor_id,
                currency="NGN"
            )
            logger.info("Escrow released and commission recognized for seller order %s", seller_order.id)
        except Exception as e:
            logger.error("Failed to recognize escrow release for seller order %s: %s", seller_order.id, e, exc_info=True)

    @staticmethod
    async def on_payout_succeeded(db: AsyncSession, payout: Payout) -> None:
        """Records double-entry debit to Merchant Payable and credit to Operating Bank."""
        try:
            acc_service = AccountingService(db)
            amount_naira = minor_to_naira(payout.amount)
            actor_id = payout.seller_id

            await acc_service.record_vendor_payout(
                vendor_id=payout.seller_id,
                amount=amount_naira,
                payout_id=payout.id,
                gateway_ref=payout.transfer_reference or str(payout.id),
                user_id=actor_id,
                currency="NGN"
            )
            logger.info("Accounting payout entry posted for payout %s", payout.id)
        except Exception as e:
            logger.error("Failed to post accounting entry for payout %s: %s", payout.id, e, exc_info=True)

    @staticmethod
    async def on_refund_completed(db: AsyncSession, refund: Refund, payment: Payment) -> None:
        """Records double-entry reversal entry for refunded orders."""
        try:
            acc_service = AccountingService(db)
            amount_naira = minor_to_naira(refund.amount)
            actor_id = refund.approved_by or payment.customer_id

            await acc_service.record_refund_reversal(
                payment_id=payment.id,
                order_id=payment.order_id or payment.id,
                refund_amount=amount_naira,
                reason=refund.reason or "Customer refund approved",
                user_id=actor_id,
                currency="NGN"
            )
            logger.info("Accounting refund reversal posted for refund %s", refund.id)
        except Exception as e:
            logger.error("Failed to post refund reversal for refund %s: %s", refund.id, e, exc_info=True)
