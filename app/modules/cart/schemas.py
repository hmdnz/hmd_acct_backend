from pydantic import BaseModel, Field
import uuid
from decimal import Decimal

class AddCartItemRequest(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0)

class UpdateCartItemRequest(BaseModel):
    quantity: int = Field(ge=0)  # 0 means remove

class CartItemResponse(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    thumbnail_url: str | None
    # False when this item can't be bought as it stands: product deactivated/unapproved,
    # seller suspended, or fewer in stock than the quantity in the cart.
    # Unavailable items are not counted in the cart subtotal and block checkout until removed.
    is_available: bool = True

class CartResponse(BaseModel):
    id: uuid.UUID
    items: list[CartItemResponse]
    subtotal: Decimal
    item_count: int