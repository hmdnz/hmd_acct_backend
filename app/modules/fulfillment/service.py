import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# ASSUMPTION: adjust import paths/field names to match your project.
from app.modules.auth.models import Seller, User
from app.modules.fulfillment import schemas
from app.modules.orders.models import (
    Order,
    OrderItem,
    OrderStatus,
    SellerOrder,
    SellerOrderStatus as S,
)

# Single source of truth for allowed moves. Adding Warehouse / "routed for
# delivery" stages later = add enum members + edit this dict.
TRANSITIONS: dict[S, set[S]] = {
    S.AWAITING_FULFILLMENT: {S.PROCESSING, S.CANCELLED},
    S.PROCESSING: {S.SHIPPED, S.CANCELLED},
    S.SHIPPED: {S.DELIVERED},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summary(so: SellerOrder, item_count: int) -> schemas.SellerOrderSummary:
    return schemas.SellerOrderSummary(
        id=so.id,
        order_id=so.order_id,
        status=so.status,
        subtotal=so.subtotal,
        item_count=item_count,
        created_at=so.created_at,
    )


# ---------- reads ----------

async def list_seller_orders(
    db: AsyncSession,
    seller: Seller,
    status_filter: S | None,
    limit: int,
    offset: int,
) -> schemas.SellerOrderList:
    conds = [SellerOrder.seller_id == seller.id, Order.status == OrderStatus.PAID]
    if status_filter:
        conds.append(SellerOrder.status == status_filter)

    total = (
        await db.execute(
            select(func.count(SellerOrder.id))
            .select_from(SellerOrder)
            .join(Order, SellerOrder.order_id == Order.id)
            .where(*conds)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(SellerOrder)
            .join(Order, SellerOrder.order_id == Order.id)
            .where(*conds)
            .order_by(SellerOrder.created_at.desc(), SellerOrder.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    counts: dict[uuid.UUID, int] = {}
    if rows:
        counts = {
            sid: int(qty)
            for sid, qty in (
                await db.execute(
                    select(OrderItem.seller_order_id, func.sum(OrderItem.quantity))
                    .where(OrderItem.seller_order_id.in_([r.id for r in rows]))
                    .group_by(OrderItem.seller_order_id)
                )
            ).all()
        }

    return schemas.SellerOrderList(
        items=[_summary(r, counts.get(r.id, 0)) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_seller_order_detail(
    db: AsyncSession, seller: Seller, seller_order_id: uuid.UUID
) -> schemas.SellerOrderDetail:
    so = (
        await db.execute(
            select(SellerOrder)
            .join(Order, SellerOrder.order_id == Order.id)
            .where(
                SellerOrder.id == seller_order_id,
                SellerOrder.seller_id == seller.id,
                Order.status == OrderStatus.PAID,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not so:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return await _build_detail(db, so)


async def _build_detail(
    db: AsyncSession, so: SellerOrder
) -> schemas.SellerOrderDetail:
    items = (
        await db.execute(
            select(OrderItem).where(OrderItem.seller_order_id == so.id)
        )
    ).scalars().all()

    order = await db.get(Order, so.order_id)
    shipping = schemas.ShippingInfo(
        recipient_name=order.ship_recipient_name,
        phone_number=order.ship_phone_number,
        country=order.ship_country,
        state=order.ship_state,
        lga=order.ship_lga,
        street=order.ship_street,
        house_number=order.ship_house_number,
        landmark=order.ship_landmark,
    )

    return schemas.SellerOrderDetail(
        **_summary(so, sum(i.quantity for i in items)).model_dump(),
        items=[
            schemas.OrderItemOut(
                product_id=i.product_id,
                product_name=i.product_name,
                unit_price=i.unit_price,
                quantity=i.quantity,
                line_total=i.line_total,
            )
            for i in items
        ],
        shipping=shipping,
        tracking_number=so.tracking_number,
        carrier_name=so.carrier_name,
        shipped_at=so.shipped_at,
        delivered_at=so.delivered_at,
        cancelled_at=so.cancelled_at,
        cancellation_reason=so.cancellation_reason,
    )


# ---------- transitions ----------

async def _lock_seller_order(
    db: AsyncSession,
    seller_order_id: uuid.UUID,
    *,
    seller_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> SellerOrder:
    """Fetch with ownership check (seller OR customer) and a row lock so two
    simultaneous requests can't both apply a transition."""
    stmt = (
        select(SellerOrder)
        .join(Order, SellerOrder.order_id == Order.id)
        .where(SellerOrder.id == seller_order_id, Order.status == OrderStatus.PAID)
    )
    if seller_id:
        stmt = stmt.where(SellerOrder.seller_id == seller_id)
    if user_id:
        stmt = stmt.where(Order.user_id == user_id)

    so = (
        await db.execute(
            stmt.with_for_update(of=SellerOrder).execution_options(
                populate_existing=True
            )
        )
    ).scalar_one_or_none()
    if not so:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return so


def _ensure_transition(so: SellerOrder, target: S) -> None:
    if target not in TRANSITIONS.get(so.status, set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot move order from '{so.status.value}' to '{target.value}'",
        )


async def mark_processing(
    db: AsyncSession, seller: Seller, seller_order_id: uuid.UUID
) -> schemas.SellerOrderDetail:
    so = await _lock_seller_order(db, seller_order_id, seller_id=seller.id)
    _ensure_transition(so, S.PROCESSING)
    so.status = S.PROCESSING
    await db.commit()
    await db.refresh(so)
    return await _build_detail(db, so)


async def mark_shipped(
    db: AsyncSession,
    seller: Seller,
    seller_order_id: uuid.UUID,
    data: schemas.ShipRequest,
) -> schemas.SellerOrderDetail:
    so = await _lock_seller_order(db, seller_order_id, seller_id=seller.id)
    _ensure_transition(so, S.SHIPPED)
    so.status = S.SHIPPED
    so.shipped_at = _now()
    so.tracking_number = data.tracking_number
    so.carrier_name = data.carrier_name
    await db.commit()
    await db.refresh(so)
    return await _build_detail(db, so)


async def cancel_by_seller(
    db: AsyncSession,
    seller: Seller,
    seller_order_id: uuid.UUID,
    data: schemas.CancelRequest,
) -> schemas.SellerOrderDetail:
    so = await _lock_seller_order(db, seller_order_id, seller_id=seller.id)
    _ensure_transition(so, S.CANCELLED)
    so.status = S.CANCELLED
    so.cancelled_at = _now()
    so.cancellation_reason = data.reason
    # NOTE: the customer has already paid. Refunding this seller_order's
    # subtotal is the payments developer's job (see INTEGRATION.md).
    await db.commit()
    await db.refresh(so)
    return await _build_detail(db, so)


async def confirm_delivery_by_customer(
    db: AsyncSession, user: User, seller_order_id: uuid.UUID
) -> None:
    so = await _lock_seller_order(db, seller_order_id, user_id=user.id)
    _ensure_transition(so, S.DELIVERED)
    so.status = S.DELIVERED
    so.delivered_at = _now()
    await db.commit()
