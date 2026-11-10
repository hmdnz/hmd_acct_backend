from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_customer
from app.modules.auth.models import User
from app.modules.cart.service import get_or_create_cart
from app.modules.orders import schemas, service

router = APIRouter(prefix="/orders", tags=["orders"])


def _to_response(order) -> schemas.OrderResponse:
    return schemas.OrderResponse(
        id=order.id, status=order.status.value, overall_status=order.overall_status,
        total_amount=order.total_amount,
        seller_orders=[
            schemas.SellerOrderResponse(
                id=so.id, seller_id=so.seller_id, status=so.status.value, subtotal=so.subtotal,
                items=[
                    schemas.OrderItemResponse(
                        product_id=i.product_id, product_name=i.product_name,
                        unit_price=i.unit_price, quantity=i.quantity, line_total=i.line_total,
                    ) for i in so.items
                ],
            ) for so in order.seller_orders
        ],
    )


@router.post("/checkout", response_model=schemas.OrderResponse)
async def checkout(
    payload: schemas.CheckoutRequest, current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)
):
    cart = await get_or_create_cart(db, current_user.id, None)
    try:
        order = await service.initiate_checkout(db, current_user.id, cart, payload.address_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _to_response(order)


@router.get("", response_model=list[schemas.OrderResponse])
async def get_my_orders(current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    orders = await service.list_my_orders(db, current_user.id)
    return [_to_response(o) for o in orders]


@router.get("/{order_id}", response_model=schemas.OrderResponse)
async def get_order_detail(
    order_id: uuid.UUID, current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)
):
    try:
        order = await service.get_order(db, current_user.id, order_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _to_response(order)