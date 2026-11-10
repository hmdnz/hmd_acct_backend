import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.modules.payments.models import PaymentProvider


class InitializePaymentRequest(BaseModel):
    """Pay for a marketplace order (order_id) or a food order (food_order_id): send exactly one."""
    order_id: uuid.UUID | None = None
    food_order_id: uuid.UUID | None = None
    provider: PaymentProvider = PaymentProvider.PAYSTACK
    callback_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.order_id is None) == (self.food_order_id is None):
            raise ValueError("Send exactly one of order_id or food_order_id")
        return self


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    order_id: uuid.UUID | None
    food_order_id: uuid.UUID | None
    transaction_ref: str
    provider: PaymentProvider
    provider_ref: str | None
    amount_minor: int
    currency: str
    status: str
    checkout_url: str | None
    paid_at: datetime | None
    created_at: datetime


class VerifyPaymentRequest(BaseModel):
    transaction_ref: str = Field(min_length=5, max_length=160)


class WalletOut(BaseModel):
    seller_id: uuid.UUID
    currency: str
    available_minor: int
    pending_minor: int
    locked_minor: int


class WalletEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    entry_type: str
    direction: str
    amount_minor: int
    currency: str
    reference: str
    description: str
    created_at: datetime


class BankOut(BaseModel):
    name: str
    code: str


class BankAccountCreate(BaseModel):
    provider: PaymentProvider = PaymentProvider.PAYSTACK
    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str | None = Field(default=None, max_length=120)
    account_number: str = Field(min_length=10, max_length=20, pattern=r"^[0-9]+$")
    account_name: str | None = Field(default=None, max_length=200)
    is_default: bool = True


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    provider: PaymentProvider
    bank_code: str
    bank_name: str | None
    account_number_last4: str
    account_name: str
    provider_recipient_code: str | None
    status: str
    is_default: bool
    squad_bank_code: str | None = None  # set once Squad's lookup confirmed the account; empty means payouts use Paystack
    squad_check: str | None = None      # why Squad was or was not confirmed (only on add and re-check)


class SquadLookupRequest(BaseModel):
    bank_code: str = Field(min_length=2, max_length=20)  # a SQUAD bank code, e.g. 000013
    account_number: str = Field(min_length=10, max_length=20, pattern=r"^[0-9]+$")


class PayoutCreate(BaseModel):
    bank_account_id: uuid.UUID
    amount_minor: int = Field(gt=0)


class PayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    reference: str
    provider: PaymentProvider      # who sent the money: paystack or squad
    seller_order_id: uuid.UUID | None  # the order this payout is for (automatic payouts are one per order)
    food_order_id: uuid.UUID | None
    bank_account_id: uuid.UUID
    requested_amount_minor: int
    fee_minor: int
    net_amount_minor: int
    currency: str
    status: str
    provider_reference: str | None
    failure_reason: str | None
    created_at: datetime


class RefundCreate(BaseModel):
    """A customer asks for money back on one seller's part of a marketplace order. An admin decides."""
    payment_id: uuid.UUID
    seller_order_id: uuid.UUID
    amount_minor: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class AdminRefundRequest(BaseModel):
    """An admin refunds a payment. Leave amount_minor empty for a full refund."""
    reason: str = Field(min_length=3, max_length=500)
    amount_minor: int | None = Field(default=None, gt=0)
    seller_order_id: uuid.UUID | None = None


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    payment_id: uuid.UUID
    order_id: uuid.UUID | None
    food_order_id: uuid.UUID | None
    seller_order_id: uuid.UUID | None
    reference: str
    amount_minor: int
    currency: str
    reason: str
    status: str
    admin_note: str | None
    provider_refund_reference: str | None
    created_at: datetime


class DiscrepancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    payment_id: uuid.UUID | None
    reference: str
    discrepancy_type: str
    expected_amount_minor: int | None
    actual_amount_minor: int | None
    details: str
    status: str
    created_at: datetime


# ---- riders ----

class RiderEarningsOut(BaseModel):
    unpaid_minor: int
    unpaid_deliveries: int
    paid_minor: int
    next_payout: str  # plain-words reminder of when the next payout runs


class RiderPayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    rider_id: uuid.UUID
    reference: str
    amount_minor: int
    deliveries_count: int
    currency: str
    status: str
    failure_reason: str | None
    created_at: datetime
    completed_at: datetime | None


class RiderBankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    bank_code: str
    bank_name: str | None
    account_number_last4: str
    account_name: str
    status: str
    is_default: bool


class RiderBankAccountCreate(BaseModel):
    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str | None = Field(default=None, max_length=120)
    account_number: str = Field(min_length=10, max_length=20, pattern=r"^[0-9]+$")
