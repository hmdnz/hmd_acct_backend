import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDMixin


class Review(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "reviews"

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    rating: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Moderation gate: hidden reviews are excluded from lists and aggregates.
    is_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )

    # lazy="raise" forces explicit selectinload() and avoids MissingGreenlet surprises.
    user = relationship("User", lazy="raise")

    __table_args__ = (
        # No explicit name: the Base naming convention generates it.
        UniqueConstraint("user_id", "product_id"),
        # Check constraints need a name for the convention to work.
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
    )
