import uuid
import enum
from decimal import Decimal
from sqlalchemy import String, Integer, Numeric, ForeignKey, Enum as SAEnum, DateTime, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.db.mixins import UUIDMixin, TimestampMixin
from datetime import datetime


class OrderStatus(str, enum.Enum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    CANCELLED = "cancelled"


class SellerOrderStatus(str, enum.Enum):
    AWAITING_FULFILLMENT = "awaiting_fulfillment"  # payment confirmed, seller needs to act
    PROCESSING = "processing"                       # "seller has gotten your order"
    SHIPPED = "shipped"                              # routed for delivery
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Order(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "orders"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True
    )

    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus, name="order_status"), default=OrderStatus.PENDING_PAYMENT)

    seller_orders: Mapped[list["SellerOrder"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    
    ship_recipient_name: Mapped[str] = mapped_column(String(255))
    ship_phone_number: Mapped[str] = mapped_column(String(50))
    ship_country: Mapped[str] = mapped_column(String(100))
    ship_state: Mapped[str] = mapped_column(String(100))
    ship_lga: Mapped[str] = mapped_column(String(100))
    ship_street: Mapped[str] = mapped_column(String(255))
    ship_house_number: Mapped[str] = mapped_column(String(50))
    ship_landmark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ship_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    ship_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def overall_status(self) -> str:
        if self.status == OrderStatus.PENDING_PAYMENT:
            return "pending_payment"
        if self.status == OrderStatus.CANCELLED:
            return "cancelled"

        statuses = [so.status for so in self.seller_orders if so.status != SellerOrderStatus.CANCELLED]
        if not statuses:
            return "cancelled"
        if all(s == SellerOrderStatus.DELIVERED for s in statuses):
            return "delivered"
        if any(s == SellerOrderStatus.SHIPPED for s in statuses):
            return "shipped"
        if any(s == SellerOrderStatus.PROCESSING for s in statuses):
            return "processing"
        return "awaiting_fulfillment"
    
    


class SellerOrder(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "seller_orders"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sellers.id"), index=True)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[SellerOrderStatus] = mapped_column(
        SAEnum(SellerOrderStatus, name="seller_order_status"), default=SellerOrderStatus.AWAITING_FULFILLMENT
    )

    order: Mapped["Order"] = relationship(back_populates="seller_orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="seller_order", cascade="all, delete-orphan")
    

    tracking_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    carrier_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OrderItem(Base, UUIDMixin):
    __tablename__ = "order_items"

    seller_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller_orders.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))

    product_name: Mapped[str] = mapped_column(String(200))   # snapshot — name could change later
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # snapshot — price locked at purchase time
    quantity: Mapped[int] = mapped_column(Integer)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    seller_order: Mapped["SellerOrder"] = relationship(back_populates="items")