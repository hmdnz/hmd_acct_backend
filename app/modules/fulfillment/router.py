import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

# ASSUMPTION: adjust these import paths to match your project.
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_customer, get_current_seller
from app.modules.auth.models import Seller, User
from app.modules.fulfillment import schemas, service
from app.modules.orders.models import SellerOrderStatus

router = APIRouter(tags=["fulfillment"])


# ---------- seller ----------

@router.get("/seller/orders", response_model=schemas.SellerOrderList)
async def list_my_orders(
    status_filter: SellerOrderStatus | None = Query(default=None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    return await service.list_seller_orders(db, seller, status_filter, limit, offset)


@router.get("/seller/orders/{seller_order_id}", response_model=schemas.SellerOrderDetail)
async def get_my_order(
    seller_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    return await service.get_seller_order_detail(db, seller, seller_order_id)


@router.post(
    "/seller/orders/{seller_order_id}/process",
    response_model=schemas.SellerOrderDetail,
)
async def process_order(
    seller_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    return await service.mark_processing(db, seller, seller_order_id)


@router.post(
    "/seller/orders/{seller_order_id}/ship",
    response_model=schemas.SellerOrderDetail,
)
async def ship_order(
    seller_order_id: uuid.UUID,
    payload: schemas.ShipRequest,
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    return await service.mark_shipped(db, seller, seller_order_id, payload)


@router.post(
    "/seller/orders/{seller_order_id}/cancel",
    response_model=schemas.SellerOrderDetail,
)
async def cancel_order(
    seller_order_id: uuid.UUID,
    payload: schemas.CancelRequest,
    db: AsyncSession = Depends(get_db),
    seller: Seller = Depends(get_current_seller),
):
    return await service.cancel_by_seller(db, seller, seller_order_id, payload)


# ---------- customer ----------

@router.post(
    "/orders/seller-orders/{seller_order_id}/confirm-delivery",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def confirm_delivery(
    seller_order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_customer),
):
    await service.confirm_delivery_by_customer(db, user, seller_order_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
