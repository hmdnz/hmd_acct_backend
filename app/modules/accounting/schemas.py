import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.accounting.models import (
    AccountType,
    ExpenseBillStatus,
    FiscalPeriodStatus,
    JournalEntryStatus,
    NormalBalance,
    PaymentTransactionStatus,
    ReconciliationStatus,
    RefundStatus,
    SubLedgerType,
    TaxRateType,
    TransactionType,
    VendorPayoutStatus,
)


# ---------- Account Schemas ----------

class AccountBase(BaseModel):
    code: str = Field(..., max_length=20, examples=["1010"])
    name: str = Field(..., max_length=150, examples=["Operating Settlement Bank Account"])
    type: AccountType
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    currency: str = Field(default="NGN", max_length=3)
    description: str | None = None


class AccountCreate(AccountBase):
    pass


class AccountUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    description: str | None = None
    parent_id: uuid.UUID | None = None


class AccountOut(AccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    normal_balance: NormalBalance
    created_at: datetime
    updated_at: datetime


class AccountTreeNode(AccountOut):
    children: list["AccountTreeNode"] = []
    current_balance: Decimal = Decimal("0.0000")


# ---------- Accounting Settings & Periods ----------

class AccountingSettingsUpdate(BaseModel):
    legal_entity_name: str | None = None
    functional_currency: str | None = None
    tax_identification_number: str | None = None
    standard_commission_rate: Decimal | None = Field(default=None, ge=0, le=1)
    standard_reserve_rate: Decimal | None = Field(default=None, ge=0, le=1)
    reserve_hold_days: int | None = Field(default=None, ge=0)
    require_dual_approval_payouts: bool | None = None


class AccountingSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_name: str
    functional_currency: str
    tax_identification_number: str | None
    fiscal_year_end_month: int
    standard_commission_rate: Decimal
    standard_reserve_rate: Decimal
    reserve_hold_days: int
    require_dual_approval_payouts: bool
    created_at: datetime
    updated_at: datetime


class FiscalPeriodCreate(BaseModel):
    name: str
    start_date: datetime
    end_date: datetime


class FiscalPeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    start_date: datetime
    end_date: datetime
    status: FiscalPeriodStatus
    closed_at: datetime | None
    closed_by: uuid.UUID | None


# ---------- Journal Entry & Line Schemas ----------

class JournalLineIn(BaseModel):
    account_code: str = Field(..., description="Chart of Accounts code, e.g. '1010'")
    debit: Decimal = Field(default=Decimal("0.0000"), ge=0)
    credit: Decimal = Field(default=Decimal("0.0000"), ge=0)
    sub_ledger_type: SubLedgerType = SubLedgerType.NONE
    sub_ledger_id: str | None = None
    currency: str = "NGN"
    fx_rate: Decimal = Decimal("1.000000")
    description: str | None = None


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    account_code: str | None = None
    account_name: str | None = None
    sub_ledger_type: SubLedgerType
    sub_ledger_id: str | None = None
    debit: Decimal
    credit: Decimal
    currency: str
    base_debit: Decimal
    base_credit: Decimal
    description: str | None = None


class JournalEntryCreate(BaseModel):
    transaction_type: TransactionType
    reference_id: str | None = None
    idempotency_key: str | None = None
    narration: str = Field(..., max_length=500)
    currency: str = "NGN"
    lines: list[JournalLineIn] = Field(..., min_length=2)


class JournalEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entry_number: str
    idempotency_key: str | None
    transaction_type: TransactionType
    reference_id: str | None
    narration: str
    status: JournalEntryStatus
    currency: str
    posted_at: datetime
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    reverses_entry_id: uuid.UUID | None
    reversed_by_id: uuid.UUID | None
    lines: list[JournalLineOut] = []
    total_debit: Decimal = Decimal("0.0000")
    total_credit: Decimal = Decimal("0.0000")


# ---------- Lifecycle Financial Event Requests ----------

class OrderPaymentEvent(BaseModel):
    order_id: uuid.UUID
    total_amount: Decimal = Field(..., gt=0, description="Gross order amount paid by customer")
    shipping_fee: Decimal = Field(default=Decimal("0.0000"), ge=0)
    gateway_reference: str = Field(..., description="Paystack charge reference")
    customer_id: uuid.UUID
    currency: str = "NGN"
    provider: str = "paystack"
    idempotency_key: str | None = None


class EscrowReleaseEvent(BaseModel):
    seller_order_id: uuid.UUID
    order_id: uuid.UUID
    seller_id: uuid.UUID
    subtotal: Decimal = Field(..., gt=0, description="Gross value of goods delivered")
    commission_rate: Decimal = Field(default=Decimal("0.1000"), ge=0, le=1)
    shipping_fee_earned: Decimal = Field(default=Decimal("0.0000"), ge=0)
    withholding_tax_rate: Decimal = Field(default=Decimal("0.0500"), ge=0, le=1)
    reserve_hold_rate: Decimal = Field(default=Decimal("0.0500"), ge=0, le=1)
    reserve_days: int = Field(default=14, ge=0)


class RefundEvent(BaseModel):
    order_id: uuid.UUID
    seller_order_id: uuid.UUID | None = None
    customer_id: uuid.UUID
    refund_amount: Decimal = Field(..., gt=0)
    reason: str
    seller_id: uuid.UUID | None = None


class GatewaySettlementEvent(BaseModel):
    gateway_name: str = "Paystack"
    gateway_reference: str
    gross_settlement: Decimal = Field(..., gt=0)
    gateway_fee: Decimal = Field(default=Decimal("0.0000"), ge=0)
    net_bank_amount: Decimal = Field(..., gt=0)


# ---------- Vendor Payout Schemas ----------

class VendorPayoutCreate(BaseModel):
    vendor_id: uuid.UUID
    amount: Decimal = Field(..., gt=0, description="Amount to withdraw")
    bank_account_details: dict[str, Any] = Field(..., description="Bank name, account number, account holder")


class VendorPayoutProcess(BaseModel):
    gateway_reference: str


class VendorPayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payout_reference: str
    vendor_id: uuid.UUID
    vendor_name: str | None = None
    amount: Decimal
    fee_deducted: Decimal
    net_amount: Decimal
    currency: str
    bank_account_details: str
    status: VendorPayoutStatus
    gateway_reference: str | None
    journal_entry_id: uuid.UUID | None
    failure_reason: str | None
    processed_at: datetime | None
    created_at: datetime


class VendorSubLedgerSummary(BaseModel):
    vendor_id: uuid.UUID
    vendor_name: str
    gross_sales: Decimal
    platform_commissions: Decimal
    shipping_earned: Decimal
    withholding_tax: Decimal
    refunds_deducted: Decimal
    reserves_held: Decimal
    total_payouts_completed: Decimal
    pending_payouts: Decimal
    current_withdrawable_balance: Decimal
    currency: str = "NGN"


# ---------- Expenses & Bank Reconciliation ----------

class ExpenseBillCreate(BaseModel):
    vendor_name: str
    expense_account_code: str = "5030"
    invoice_number: str | None = None
    subtotal: Decimal = Field(..., gt=0)
    tax_amount: Decimal = Field(default=Decimal("0.0000"), ge=0)
    currency: str = "NGN"
    due_date: datetime | None = None


class ExpenseBillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_name: str
    expense_account_id: uuid.UUID
    invoice_number: str | None
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    currency: str
    status: ExpenseBillStatus
    due_date: datetime | None
    paid_at: datetime | None
    journal_entry_id: uuid.UUID | None
    created_at: datetime


class BankStatementRowIn(BaseModel):
    account_code: str = "1010"
    statement_date: datetime
    reference: str
    description: str | None = None
    debit: Decimal = Decimal("0.0000")
    credit: Decimal = Decimal("0.0000")
    balance: Decimal
    currency: str = "NGN"


class BankStatementRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_code: str
    statement_date: datetime
    reference: str
    description: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    currency: str
    status: ReconciliationStatus
    matched_journal_line_id: uuid.UUID | None


# ---------- Financial Reports & Statements ----------

class ReportLineItem(BaseModel):
    code: str
    name: str
    amount: Decimal
    percentage_of_total: Decimal | None = None
    sub_lines: list["ReportLineItem"] = []


class ProfitAndLossReport(BaseModel):
    currency: str = "NGN"
    period_start: datetime | None
    period_end: datetime | None
    operating_revenues: list[ReportLineItem]
    total_revenue: Decimal
    cost_of_sales: list[ReportLineItem]
    total_cost_of_sales: Decimal
    gross_profit: Decimal
    gross_profit_margin_pct: Decimal
    operating_expenses: list[ReportLineItem]
    total_operating_expenses: Decimal
    net_profit: Decimal
    net_profit_margin_pct: Decimal


class BalanceSheetReport(BaseModel):
    currency: str = "NGN"
    as_of_date: datetime
    current_assets: list[ReportLineItem]
    total_assets: Decimal
    current_liabilities: list[ReportLineItem]
    total_liabilities: Decimal
    equity_items: list[ReportLineItem]
    current_period_earnings: Decimal
    total_equity: Decimal
    total_liabilities_and_equity: Decimal
    is_balanced: bool
    imbalance: Decimal


class CashFlowReport(BaseModel):
    currency: str = "NGN"
    method: str = "INDIRECT"
    as_of_date: datetime
    period_start: datetime | None
    operating_cash_flow: list[ReportLineItem]
    net_cash_from_operating: Decimal
    investing_cash_flow: list[ReportLineItem]
    net_cash_from_investing: Decimal
    financing_cash_flow: list[ReportLineItem]
    net_cash_from_financing: Decimal
    net_change_in_cash: Decimal
    beginning_cash_balance: Decimal
    ending_cash_balance: Decimal


class TrialBalanceItem(BaseModel):
    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    debit_balance: Decimal
    credit_balance: Decimal


class TrialBalanceReport(BaseModel):
    currency: str = "NGN"
    as_of_date: datetime
    items: list[TrialBalanceItem]
    total_debits: Decimal
    total_credits: Decimal
    is_balanced: bool
    imbalance: Decimal


class AccountingDashboardKPIs(BaseModel):
    gmv: Decimal
    net_platform_revenue: Decimal
    escrow_holding: Decimal
    pending_vendor_payouts: Decimal
    settlement_health_ratio: float
    operating_bank_balance: Decimal
    gateway_clearing_balance: Decimal
    total_merchant_payables: Decimal
    active_reserve_holds: Decimal
    total_journal_entries_count: int
    unbalanced_entries_count: int = 0
