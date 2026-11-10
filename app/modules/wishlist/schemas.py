import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class WishlistAdd(BaseModel):
    product_id: uuid.UUID


class WishlistItemOut(BaseModel):
    product_id: uuid.UUID
    name: str
    price: Decimal
    compare_at_price: Decimal | None
    average_rating: Decimal
    review_count: int
    image_url: str | None
    in_stock: bool
    is_available: bool  # False if product was deactivated/unapproved after being saved
    added_at: datetime


class WishlistOut(BaseModel):
    items: list[WishlistItemOut]
    total: int


class WishlistProductIds(BaseModel):
    """Lightweight payload so product grids can render filled/empty hearts."""

    product_ids: list[uuid.UUID]
