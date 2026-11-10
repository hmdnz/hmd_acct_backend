import uuid
from sqlalchemy import String, Integer, Numeric, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.db.mixins import UUIDMixin, TimestampMixin
from decimal import Decimal
from app.modules.catalog.s3_service import get_public_url


class Category(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id"), nullable=True)

    parent: Mapped["Category | None"] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")


class Brand(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)


class Tag(Base, UUIDMixin):
    __tablename__ = "tags"
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)


class ProductTag(Base):
    __tablename__ = "product_tags"
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tags.id"), primary_key=True)


class Product(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "products"

    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id"), index=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"), nullable=True)

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    price: Mapped[float] = mapped_column(Numeric(12, 2))
    compare_at_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)  # original price, to show a strikethrough discount
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)   # seller can hide/show it
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)  # admin moderation gate, same pattern as seller approval

    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product", order_by="ProductImage.display_order", cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(secondary="product_tags")
    average_rating: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), default=Decimal("0"), server_default="0"
    )
    review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    
    @property
    def image_urls(self) -> list[str]:
        return [
            get_public_url(img.s3_key)
            for img in sorted(self.images, key=lambda i: i.display_order)
        ]

class ProductImage(Base, UUIDMixin):
    __tablename__ = "product_images"

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    s3_key: Mapped[str] = mapped_column(String(500))
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped["Product"] = relationship(back_populates="images")