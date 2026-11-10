import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, ForeignKey, DateTime, Boolean, Text, Enum as SAEnum, UniqueConstraint, Index, CheckConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.db.mixins import UUIDMixin, TimestampMixin


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentProvider(str, enum.Enum):
    PAYSTACK = "paystack"
    SQUAD = "squad"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class AllocationStatus(str, enum.Enum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    RELEASED = "released"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class WalletEntryType(str, enum.Enum):
    SELLER_EARNING_PENDING = "seller_earning_pending"
    SELLER_FUNDS_RELEASED = "seller_funds_released"
    PAYOUT_RESERVATION = "payout_reservation"
    PAYOUT_SUCCESS = "payout_success"
    PAYOUT_FAILED_REVERSAL = "payout_failed_reversal"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class PayoutStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    REVERSED = "reversed"
    CANCELLED = "cancelled"


class RefundStatus(str, enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    REJECTED = "rejected"


class BankAccountStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    DISABLED = "disabled"


class PaymentDiscrepancyStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class Payment(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("transaction_ref", name="uq_payments_transaction_ref"),
        # a payment is for exactly one thing: a marketplace order OR a food order
        CheckConstraint("(order_id IS NOT NULL) <> (food_order_id IS NOT NULL)", name="one_target"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True, index=True)
    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, name="payment_provider"))
    transaction_ref: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[PaymentStatus] = mapped_column(SAEnum(PaymentStatus, name="payment_status"), default=PaymentStatus.PENDING, index=True)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentAllocation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "seller_order_id", name="uq_payment_allocations_payment_seller_order"),
        UniqueConstraint("payment_id", "food_order_id", name="uq_payment_allocations_payment_food_order"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    # marketplace allocations set order_id + seller_order_id; food allocations set food_order_id
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    seller_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seller_orders.id"), nullable=True, index=True)
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True, index=True)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    gross_amount_minor: Mapped[int] = mapped_column(BigInteger)
    refunded_gross_minor: Mapped[int] = mapped_column(BigInteger, default=0)  # how much of gross was refunded to the customer
    platform_commission_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    tax_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    seller_net_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[AllocationStatus] = mapped_column(SAEnum(AllocationStatus, name="allocation_status"), default=AllocationStatus.PENDING, index=True)
    eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SellerWallet(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "seller_wallets"
    __table_args__ = (UniqueConstraint("seller_id", "currency", name="uq_seller_wallets_seller_currency"),)

    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    available_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    pending_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    locked_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    lifetime_credits_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    lifetime_debits_minor: Mapped[int] = mapped_column(BigInteger, default=0)


class WalletEntry(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "wallet_entries"
    __table_args__ = (Index("ix_wallet_entries_wallet_created", "wallet_id", "created_at"),)

    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller_wallets.id"), index=True)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    entry_type: Mapped[WalletEntryType] = mapped_column(SAEnum(WalletEntryType, name="wallet_entry_type"), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"), nullable=True, index=True)
    allocation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_allocations.id"), nullable=True, index=True)
    payout_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payouts.id"), nullable=True, index=True)
    refund_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refunds.id"), nullable=True, index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    seller_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seller_orders.id"), nullable=True, index=True)
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True, index=True)
    reference: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[str] = mapped_column(String(500))


class PaymentProviderEvent(Base, UUIDMixin):
    __tablename__ = "payment_provider_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_payment_provider_events_provider_event"),)

    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, name="provider_event_provider"))
    event_id: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(120))
    payment_reference: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(128))
    payload: Mapped[str] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SellerBankAccount(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "seller_bank_accounts"
    __table_args__ = (UniqueConstraint("seller_id", "account_number_hash", name="uq_seller_bank_accounts_seller_account"),)

    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, name="bank_account_provider"))
    bank_code: Mapped[str] = mapped_column(String(20))  # the Paystack bank code
    # Squad uses its own code for the same bank. Filled in (and checked with Squad's account lookup) when the
    # account is saved; empty means Squad payouts are not possible for this account.
    squad_bank_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_number_encrypted: Mapped[str] = mapped_column(Text)
    account_number_hash: Mapped[str] = mapped_column(String(128), index=True)
    account_number_last4: Mapped[str] = mapped_column(String(4))
    account_name: Mapped[str] = mapped_column(String(200))
    provider_recipient_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[BankAccountStatus] = mapped_column(SAEnum(BankAccountStatus, name="bank_account_status"), default=BankAccountStatus.PENDING)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class Payout(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "payouts"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payouts_reference"),
        # An order's money is paid out once: at most one live (not failed/reversed) payout per allocation.
        Index(
            "uq_payouts_one_live_per_allocation", "allocation_id", unique=True,
            postgresql_where=text("allocation_id IS NOT NULL AND status IN ('REQUESTED','APPROVED','PROCESSING','SUCCESSFUL')"),
        ),
    )

    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    # Automatic payouts are one per order: these say which. Empty for a manual withdrawal.
    allocation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_allocations.id"), nullable=True)
    seller_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seller_orders.id"), nullable=True, index=True)
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True, index=True)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller_wallets.id"), index=True)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller_bank_accounts.id"), index=True)
    reference: Mapped[str] = mapped_column(String(160), index=True)
    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, name="payout_provider"))
    provider_reference: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    requested_amount_minor: Mapped[int] = mapped_column(BigInteger)
    fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[PayoutStatus] = mapped_column(SAEnum(PayoutStatus, name="payout_status"), default=PayoutStatus.REQUESTED, index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Refund(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "refunds"
    __table_args__ = (UniqueConstraint("reference", name="uq_refunds_reference"),)

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True, index=True)
    seller_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seller_orders.id"), nullable=True, index=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"), nullable=True)
    reference: Mapped[str] = mapped_column(String(160), index=True)
    provider_refund_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[RefundStatus] = mapped_column(SAEnum(RefundStatus, name="refund_status"), default=RefundStatus.REQUESTED, index=True)
    admin_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentDiscrepancy(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "payment_discrepancies"

    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"), nullable=True, index=True)
    provider: Mapped[PaymentProvider] = mapped_column(SAEnum(PaymentProvider, name="discrepancy_provider"))
    reference: Mapped[str] = mapped_column(String(160), index=True)
    discrepancy_type: Mapped[str] = mapped_column(String(100))
    expected_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    details: Mapped[str] = mapped_column(Text)
    status: Mapped[PaymentDiscrepancyStatus] = mapped_column(SAEnum(PaymentDiscrepancyStatus, name="discrepancy_status"), default=PaymentDiscrepancyStatus.OPEN, index=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Food riders: bank account, per-delivery earnings, and the daily payout
# ---------------------------------------------------------------------------

class RiderBankAccount(Base, UUIDMixin, TimestampMixin):
    """Where a rider is paid. Riders are paid through Paystack transfers."""
    __tablename__ = "rider_bank_accounts"
    __table_args__ = (UniqueConstraint("rider_id", "account_number_hash", name="uq_rider_bank_accounts_rider_account"),)

    rider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("riders.id"), index=True)
    bank_code: Mapped[str] = mapped_column(String(20))
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_number_encrypted: Mapped[str] = mapped_column(Text)
    account_number_hash: Mapped[str] = mapped_column(String(128), index=True)
    account_number_last4: Mapped[str] = mapped_column(String(4))
    account_name: Mapped[str] = mapped_column(String(200))
    provider_recipient_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[BankAccountStatus] = mapped_column(SAEnum(BankAccountStatus, name="rider_bank_account_status"), default=BankAccountStatus.PENDING)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)


class RiderPayout(Base, UUIDMixin, TimestampMixin):
    """One rider's pay for one day: the sum of the delivery fees they earned since the last payout."""
    __tablename__ = "rider_payouts"
    __table_args__ = (UniqueConstraint("reference", name="uq_rider_payouts_reference"),)

    rider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("riders.id"), index=True)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rider_bank_accounts.id"), index=True)
    reference: Mapped[str] = mapped_column(String(160), index=True)
    provider_reference: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    deliveries_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[PayoutStatus] = mapped_column(SAEnum(PayoutStatus, name="rider_payout_status"), default=PayoutStatus.REQUESTED, index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admins.id"), nullable=True)
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiderEarning(Base, UUIDMixin, TimestampMixin):
    """The rider's share of one completed delivery. payout_id is empty until it is included in a payout."""
    __tablename__ = "rider_earnings"
    __table_args__ = (
        UniqueConstraint("food_order_id", name="uq_rider_earnings_food_order_id"),
        UniqueConstraint("parcel_id", name="uq_rider_earnings_parcel_id"),
        UniqueConstraint("pickup_job_id", name="uq_rider_earnings_pickup_job_id"),
    )

    rider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("riders.id"), index=True)
    # exactly one of these says what the money is for: a food delivery, a market parcel delivery or a market pickup trip
    food_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food_orders.id"), nullable=True)
    parcel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("parcels.id"), nullable=True)
    pickup_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pickup_jobs.id"), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    payout_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rider_payouts.id"), nullable=True, index=True)
