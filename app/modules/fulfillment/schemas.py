import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

# ASSUMPTION: adjust import path to match your project.
from app.modules.orders.models import SellerOrderStatus


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class ShipRequest(BaseModel):
    tracking_number: str | None = Field(default=None, max_length=100)
    carrier_name: str | None = Field(default=None, max_length=100)

    _strip = field_validator("tracking_number", "carrier_name")(_clean)


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=255)

    _strip = field_validator("reason")(lambda v: v.strip())


class OrderItemOut(BaseModel):
    product_id: uuid.UUID
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class ShippingInfo(BaseModel):
    recipient_name: str
    phone_number: str
    country: str
    state: str
    lga: str
    street: str
    house_number: str
    landmark: str | None


class SellerOrderSummary(BaseModel):
    id: uuid.UUID  # seller_order id
    order_id: uuid.UUID  # the customer-facing order
    status: SellerOrderStatus
    subtotal: Decimal
    item_count: int
    created_at: datetime


class SellerOrderList(BaseModel):
    items: list[SellerOrderSummary]
    total: int
    limit: int
    offset: int


class SellerOrderDetail(SellerOrderSummary):
    items: list[OrderItemOut]
    shipping: ShippingInfo | None  # None if the address no longer exists
    tracking_number: str | None
    carrier_name: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
