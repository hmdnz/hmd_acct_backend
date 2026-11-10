import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin


class Wishlist(Base, UUIDMixin, TimestampMixin):
    """Same ownership shape as Cart: user_id XOR guest_token."""

    __tablename__ = "wishlists"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    guest_token: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )

    items = relationship(
        "WishlistItem", back_populates="wishlist", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) <> (guest_token IS NOT NULL)", name="owner_xor"
        ),
    )


class WishlistItem(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "wishlist_items"

    wishlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wishlists.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )

    wishlist = relationship("Wishlist", back_populates="items")
    # lazy="raise": must be eager-loaded with selectinload (avoids MissingGreenlet)
    product = relationship("Product", lazy="raise")

    __table_args__ = (UniqueConstraint("wishlist_id", "product_id"),)
