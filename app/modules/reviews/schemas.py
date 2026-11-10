import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=120)
    comment: str | None = Field(default=None, max_length=2000)

    _strip = field_validator("title", "comment")(_clean)


class ReviewUpdate(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    title: str | None = Field(default=None, max_length=120)
    comment: str | None = Field(default=None, max_length=2000)

    _strip = field_validator("title", "comment")(_clean)


class ReviewOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    rating: int
    title: str | None
    comment: str | None
    reviewer_name: str  # "Amina B."
    is_own: bool = False
    created_at: datetime
    updated_at: datetime


class ReviewListOut(BaseModel):
    items: list[ReviewOut]
    total: int
    limit: int
    offset: int


class RatingSummary(BaseModel):
    average_rating: Decimal
    review_count: int
    # {"5": 10, "4": 3, "3": 0, "2": 1, "1": 0}
    distribution: dict[str, int]


ReviewSort = Literal["newest", "highest", "lowest"]
