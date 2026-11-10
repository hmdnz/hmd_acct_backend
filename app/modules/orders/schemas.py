from pydantic import BaseModel
from decimal import Decimal
import uuid

class CheckoutRequest(BaseModel):
    address_id: uuid.UUID

class OrderItemResponse(BaseModel):
    product_id: uuid.UUID
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal

class SellerOrderResponse(BaseModel):
    id: uuid.UUID
    seller_id: uuid.UUID
    status: str
    subtotal: Decimal
    items: list[OrderItemResponse]

class OrderResponse(BaseModel):
    id: uuid.UUID
    status: str
    overall_status: str
    total_amount: Decimal
    seller_orders: list[SellerOrderResponse]