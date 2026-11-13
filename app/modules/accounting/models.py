import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, relationship

try:
    from sqlalchemy.orm import mapped_column
except ImportError:
    from sqlalchemy import Column as mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# ENUMS
# ============================================================================

class AccountType(str, enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class NormalBalance(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class JournalEntryStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class TransactionType(str, enum.Enum):
    ORDER_PAYMENT = "ORDER_PAYMENT"
    ESCROW_RELEASE = "ESCROW_RELEASE"
    VENDOR_PAYOUT = "VENDOR_PAYOUT"
    REFUND = "REFUND"
    DISPUTE_HOLD = "DISPUTE_HOLD"
    RESERVE_RELEASE = "RESERVE_RELEASE"
    GATEWAY_SETTLEMENT = "GATEWAY_SETTLEMENT"
    EXPENSE_PAYMENT = "EXPENSE_PAYMENT"
    ADJUSTMENT = "ADJUSTMENT"
    OPENING_BALANCE = "OPENING_BALANCE"


class SubLedgerType(str, enum.Enum):
    VENDOR = "VENDOR"
    CUSTOMER = "CUSTOMER"
    GATEWAY = "GATEWAY"
    EXPENSE_VENDOR = "EXPENSE_VENDOR"
    NONE = "NONE"


class FiscalPeriodStatus(str, enum.Enum):
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    CLOSED = "CLOSED"


class PaymentTransactionStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    DISPUTED = "DISPUTED"


class RefundStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class VendorPayoutStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExpenseBillStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class TaxRateType(str, enum.Enum):
    VAT = "VAT"
    WHT = "WHT"
    SALES_TAX = "SALES_TAX"


class ReconciliationStatus(str, enum.Enum):
    UNMATCHED = "UNMATCHED"
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"


# ============================================================================
# 1. ACCOUNTING ENTITY & CONFIGURATION MODELS
# ============================================================================

class AccountingSettings(Base, UUIDMixin, TimestampMixin):
    """
    Reporting legal entity global accounting profile.
    """
    __tablename__ = "accounting_settings"

    legal_entity_name: Mapped[str] = mapped_column(String(200), default="Hamads Marketplace Ltd", nullable=False)
    functional_currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    tax_identification_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fiscal_year_end_month: Mapped[int] = mapped_column(default=12, nullable=False)  # December
    standard_commission_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.1000"), nullable=False)
    standard_reserve_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.0500"), nullable=False)
    reserve_hold_days: Mapped[int] = mapped_column(default=14, nullable=False)
    require_dual_approval_payouts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FiscalPeriod(Base, UUIDMixin, TimestampMixin):
    """
    Controls monthly/quarterly accounting lockouts.
    """
    __tablename__ = "accounting_fiscal_periods"

    name: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "2026-11"
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[FiscalPeriodStatus] = mapped_column(
        SAEnum(FiscalPeriodStatus, name="fiscal_period_status"), default=FiscalPeriodStatus.OPEN, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


# ============================================================================
# 2. CHART OF ACCOUNTS & GENERAL LEDGER MODELS
# ============================================================================

class Account(Base, UUIDMixin, TimestampMixin):
    """
    IFRS Chart of Accounts (CoA) Architecture.
    Standard Structure:
      1000s: Assets (Normal Debit)
      2000s: Liabilities (Normal Credit)
      3000s: Equity (Normal Credit)
      4000s: Revenue (Normal Credit - IFRS 15 Agent Recognition)
      5000s: Expenses (Normal Debit)
    """
    __tablename__ = "accounts"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    type: Mapped[AccountType] = mapped_column(SAEnum(AccountType, name="accounting_account_type"), nullable=False, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    parent: Mapped["Account | None"] = relationship("Account", remote_side="Account.id", back_populates="children")
    children: Mapped[list["Account"]] = relationship("Account", back_populates="parent")
    journal_lines: Mapped[list["JournalLine"]] = relationship("JournalLine", back_populates="account")

    @property
    def normal_balance(self) -> NormalBalance:
        if self.type in (AccountType.ASSET, AccountType.EXPENSE):
            return NormalBalance.DEBIT
        return NormalBalance.CREDIT


class JournalEntry(Base, UUIDMixin, TimestampMixin):
    """
    Double-entry journal header.
    IMMUTABLE once posted. Reversals create explicit offset entries.
    """
    __tablename__ = "journal_entries"

    entry_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(150), unique=True, index=True, nullable=True)
    transaction_type: Mapped[TransactionType] = mapped_column(
        SAEnum(TransactionType, name="transaction_type"), nullable=False, index=True
    )
    reference_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    narration: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[JournalEntryStatus] = mapped_column(
        SAEnum(JournalEntryStatus, name="journal_entry_status"),
        default=JournalEntryStatus.POSTED,
        nullable=False,
        index=True,
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    reversed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )

    lines: Mapped[list["JournalLine"]] = relationship(
        "JournalLine", back_populates="journal_entry", cascade="all, delete-orphan"
    )


class JournalLine(Base, UUIDMixin):
    """
    Debit or Credit line posting. Precision: NUMERIC(18, 4).
    """
    __tablename__ = "journal_lines"

    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    sub_ledger_type: Mapped[SubLedgerType] = mapped_column(
        SAEnum(SubLedgerType, name="sub_ledger_type"),
        default=SubLedgerType.NONE,
        nullable=False,
        index=True,
    )
    sub_ledger_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("1.000000"), nullable=False)
    base_debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    base_credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    journal_entry: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="lines")
    account: Mapped["Account"] = relationship("Account", back_populates="journal_lines")


# ============================================================================
# 3. PAYMENT GATEWAY TRANSACTIONS & REFUNDS
# ============================================================================

class PaymentTransaction(Base, UUIDMixin, TimestampMixin):
    """
    Tracks authoritative provider collections (Paystack/Stripe/OPay).
    """
    __tablename__ = "payment_transactions"

    provider: Mapped[str] = mapped_column(String(50), default="paystack", nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    provider_fee: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[PaymentTransactionStatus] = mapped_column(
        SAEnum(PaymentTransactionStatus, name="payment_transaction_status"),
        default=PaymentTransactionStatus.INITIATED,
        nullable=False,
        index=True,
    )
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)


class RefundRecord(Base, UUIDMixin, TimestampMixin):
    """
    Customer refund records linked to payment transactions and double-entry reversal.
    """
    __tablename__ = "accounting_refunds"

    payment_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    seller_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seller_orders.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[RefundStatus] = mapped_column(
        SAEnum(RefundStatus, name="refund_status"), default=RefundStatus.PENDING, nullable=False
    )
    provider_refund_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


# ============================================================================
# 4. VENDOR PAYOUTS & DISPUTE RESERVES
# ============================================================================

class VendorPayout(Base, UUIDMixin, TimestampMixin):
    """
    Vendor withdrawal and bank disbursement records. Protected by row locking.
    """
    __tablename__ = "vendor_payouts"

    payout_reference: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fee_deducted: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    bank_account_details: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[VendorPayoutStatus] = mapped_column(
        SAEnum(VendorPayoutStatus, name="vendor_payout_status"),
        default=VendorPayoutStatus.PENDING,
        nullable=False,
        index=True,
    )
    gateway_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


class VendorReserveHold(Base, UUIDMixin, TimestampMixin):
    """
    Dispute risk management reserves held from delivered seller proceeds.
    """
    __tablename__ = "vendor_reserve_holds"

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    reference_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    hold_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    released_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="HELD", nullable=False)
    release_due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )


# ============================================================================
# 5. EXPENSES & BANK RECONCILIATION
# ============================================================================

class ExpenseBill(Base, UUIDMixin, TimestampMixin):
    """
    Corporate platform operating bills (AWS, Termii, Logistics, Rent).
    """
    __tablename__ = "accounting_expenses"

    vendor_name: Mapped[str] = mapped_column(String(150), nullable=False)
    expense_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    status: Mapped[ExpenseBillStatus] = mapped_column(
        SAEnum(ExpenseBillStatus, name="expense_bill_status"), default=ExpenseBillStatus.DRAFT, nullable=False
    )
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


class TaxRate(Base, UUIDMixin, TimestampMixin):
    """
    Tax rates for Value Added Tax (VAT) and Withholding Tax (WHT).
    """
    __tablename__ = "tax_rates"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), default="NG", nullable=False)
    type: Mapped[TaxRateType] = mapped_column(SAEnum(TaxRateType, name="tax_rate_type"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BankStatementRow(Base, UUIDMixin, TimestampMixin):
    """
    External bank/provider statement lines for reconciliation.
    """
    __tablename__ = "bank_statement_rows"

    account_code: Mapped[str] = mapped_column(String(20), default="1010", nullable=False, index=True)
    statement_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.0000"), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    status: Mapped[ReconciliationStatus] = mapped_column(
        SAEnum(ReconciliationStatus, name="reconciliation_status"),
        default=ReconciliationStatus.UNMATCHED,
        nullable=False,
        index=True,
    )
    matched_journal_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_lines.id", ondelete="SET NULL"), nullable=True
    )
