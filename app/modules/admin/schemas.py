import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.auth.models import AccountType

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------- auth ----------

class AdminLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class AdminMe(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str


# ---------- shared ----------

class _WithReason(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


# ---------- sellers ----------

class AdminSellerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    date_of_birth: date
    phone_number: str
    email: str
    account_type: AccountType
    brand_name: str
    business_address: str
    is_phone_verified: bool
    is_email_verified: bool
    is_approved: bool
    is_active: bool
    created_at: datetime


class AdminSellerUpdate(_WithReason):
    is_approved: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _need_one(self):
        if self.is_approved is None and self.is_active is None:
            raise ValueError("Send is_approved and/or is_active")
        return self


# ---------- customers ----------

class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    phone_number: str
    email: str | None
    is_phone_verified: bool
    is_email_verified: bool
    is_active: bool
    created_at: datetime


class AdminUserUpdate(_WithReason):
    is_active: bool


# ---------- products ----------

class AdminProductOut(BaseModel):
    id: uuid.UUID
    seller_id: uuid.UUID
    store_name: str
    name: str
    slug: str
    description: str | None
    price: Decimal
    compare_at_price: Decimal | None
    stock_quantity: int
    category_id: uuid.UUID
    brand_id: uuid.UUID | None
    image_urls: list[str]
    average_rating: Decimal
    review_count: int
    is_active: bool
    is_approved: bool
    created_at: datetime


class AdminProductUpdate(_WithReason):
    is_approved: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _need_one(self):
        if self.is_approved is None and self.is_active is None:
            raise ValueError("Send is_approved and/or is_active")
        return self


# ---------- reviews ----------

class AdminReviewOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    user_id: uuid.UUID
    reviewer_name: str
    rating: int
    title: str | None
    comment: str | None
    is_visible: bool
    created_at: datetime


class AdminReviewUpdate(_WithReason):
    is_visible: bool


# ---------- categories / brands ----------

class NameRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Name is too short")
        return v


class CategoryCreate(NameRequest):
    parent_id: uuid.UUID | None = None


class BrandCreate(NameRequest):
    pass


# ---------- audit log ----------

class AdminActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admin_id: uuid.UUID
    action: str
    target_type: str
    target_id: uuid.UUID
    detail: str | None
    reason: str | None
    created_at: datetime