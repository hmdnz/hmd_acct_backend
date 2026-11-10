from pydantic import BaseModel, ConfigDict, Field  # CHANGED: ConfigDict added
from decimal import Decimal
import uuid


class PresignedUploadBatchRequest(BaseModel):
    content_types: list[str] = Field(min_length=1, max_length=10)

class PresignedUploadItem(BaseModel):
    upload_url: str
    s3_key: str

class PresignedUploadBatchResponse(BaseModel):
    uploads: list[PresignedUploadItem]


class ProductCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    category_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    compare_at_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    stock_quantity: int = Field(ge=0)
    image_keys: list[str] = Field(default_factory=list, max_length=10)
    tag_names: list[str] = Field(default_factory=list, max_length=20)


class ProductResponse(BaseModel):
    # CHANGED: v2-style config (class Config is deprecated in Pydantic v2)
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    price: Decimal
    compare_at_price: Decimal | None
    stock_quantity: int
    is_active: bool
    is_approved: bool
    image_urls: list[str] = []
    average_rating: Decimal = Decimal("0")  # NEW (defaults keep the manual construction in create_product working)
    review_count: int = 0  # NEW


class ProductListItem(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    price: Decimal
    compare_at_price: Decimal | None
    thumbnail_url: str | None
    average_rating: Decimal = Decimal("0")  # NEW
    review_count: int = 0  # NEW

class ProductListResponse(BaseModel):
    items: list[ProductListItem]
    total: int
    page: int
    page_size: int

class ProductDetailResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    price: Decimal
    compare_at_price: Decimal | None
    stock_quantity: int
    category_id: uuid.UUID
    brand_id: uuid.UUID | None
    image_urls: list[str]
    tag_names: list[str]
    brand_name: str  # the seller's shop name, shown to shoppers
    average_rating: Decimal = Decimal("0")  # NEW
    review_count: int = 0  # NEW

class CategoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    parent_id: uuid.UUID | None

class BrandResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class ProductUpdateRequest(BaseModel):
    """Every field optional. Only fields the client actually sends are changed
    (the service uses model_dump(exclude_unset=True))."""

    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None  # send null to remove the brand
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    compare_at_price: Decimal | None = Field(  # send null to clear the discount
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None  # false = hide listing, true = relist
    image_keys: list[str] | None = Field(default=None, min_length=1, max_length=10)
    tag_names: list[str] | None = Field(default=None, max_length=20)