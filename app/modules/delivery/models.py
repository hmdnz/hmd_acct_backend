import uuid
from sqlalchemy import String, Boolean, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.db.mixins import UUIDMixin, TimestampMixin


class Address(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "addresses"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)

    country: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(100))
    lga: Mapped[str] = mapped_column(String(100))  # Local Government Area
    street: Mapped[str] = mapped_column(String(255))
    house_number: Mapped[str] = mapped_column(String(50))
    landmark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    google_place_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)