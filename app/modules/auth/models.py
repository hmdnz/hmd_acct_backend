import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, ForeignKey, DateTime, Integer, Enum as SAEnum, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.db.mixins import UUIDMixin, TimestampMixin
from datetime import date

class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255))

    is_phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class AccountType(str, enum.Enum):
    INDIVIDUAL = "individual"
    COMPANY = "company"

class Seller(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sellers"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date] = mapped_column(Date)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))

    account_type: Mapped[AccountType] = mapped_column(SAEnum(AccountType, name="account_type"))
    brand_name: Mapped[str] = mapped_column(String(150))
    business_address: Mapped[str] = mapped_column(String(500))  # doubles as "location" for now

    is_phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EmailVerificationCode(Base, UUIDMixin):
    __tablename__ = "email_verification_codes"

    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"))
    hashed_code: Mapped[str] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VerificationChannel(str, enum.Enum):
    PHONE = "phone"
    EMAIL = "email"

class VerificationPurpose(str, enum.Enum):
    CUSTOMER_REGISTRATION = "customer_registration"
    SELLER_REGISTRATION = "seller_registration"
    PASSWORD_RESET = "password_reset"

class VerificationCode(Base, UUIDMixin):
    __tablename__ = "verification_codes"

    identifier: Mapped[str] = mapped_column(String(255), index=True)  # a phone number OR an email
    channel: Mapped[VerificationChannel] = mapped_column(SAEnum(VerificationChannel, name="verification_channel"))
    purpose: Mapped[VerificationPurpose] = mapped_column(SAEnum(VerificationPurpose, name="verification_purpose"))
    hashed_code: Mapped[str] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    
class Admin(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "admins"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminAction(Base, UUIDMixin, TimestampMixin):
    """Audit log: one row per admin change, who/what/when/why."""
    __tablename__ = "admin_actions"

    admin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("admins.id"), index=True)
    action: Mapped[str] = mapped_column(String(60))          # e.g. "seller.update"
    target_type: Mapped[str] = mapped_column(String(30), index=True)  # seller / product / review ...
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)  # "is_approved: False -> True"
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)