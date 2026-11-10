from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal
from app.db.session import get_db
from app.modules.cart.dependencies import get_cart_identity
from app.modules.cart import schemas, service
from app.modules.catalog.models import Product
from app.modules.catalog.s3_service import get_public_url
from app.modules.catalog.seller_check import unavailable_seller_ids
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import uuid

router = APIRouter(prefix="/cart", tags=["cart"])



async def _build_cart_response(db: AsyncSession, cart) -> schemas.CartResponse:
    if not cart.items:
        return schemas.CartResponse(id=cart.id, items=[], subtotal=Decimal("0"), item_count=0)

    product_ids = [item.product_id for item in cart.items]
    result = await db.execute(
        select(Product).options(selectinload(Product.images)).where(Product.id.in_(product_ids))
    )
    products_by_id = {p.id: p for p in result.scalars().all()}
    blocked_sellers = await unavailable_seller_ids(db, {p.seller_id for p in products_by_id.values()})

    items = []
    subtotal = Decimal("0")
    for item in cart.items:
        product = products_by_id.get(item.product_id)
        if not product:
            continue  # product was deleted after being added to cart; skip it safely
        line_total = product.price * item.quantity
        is_available = (
            product.is_active and product.is_approved
            and product.seller_id not in blocked_sellers
            and item.quantity <= product.stock_quantity
        )
        if is_available:
            subtotal += line_total
        items.append(schemas.CartItemResponse(
            id=item.id, product_id=product.id, product_name=product.name,
            unit_price=product.price, quantity=item.quantity, line_total=line_total,
            thumbnail_url=get_public_url(product.images[0].s3_key) if product.images else None,
            is_available=is_available,
        ))
    return schemas.CartResponse(id=cart.id, items=items, subtotal=subtotal, item_count=len(items))


@router.get("", response_model=schemas.CartResponse)
async def get_cart(identity: dict = Depends(get_cart_identity), db: AsyncSession = Depends(get_db)):
    cart = await service.get_or_create_cart(db, identity["user_id"], identity["guest_token"])
    return await _build_cart_response(db, cart)


@router.post("/items", response_model=schemas.CartResponse)
async def add_to_cart(
    payload: schemas.AddCartItemRequest, identity: dict = Depends(get_cart_identity), db: AsyncSession = Depends(get_db)
):
    cart = await service.get_or_create_cart(db, identity["user_id"], identity["guest_token"])
    try:
        cart = await service.add_item(db, cart, payload.product_id, payload.quantity)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await _build_cart_response(db, cart)


@router.patch("/items/{item_id}", response_model=schemas.CartResponse)
async def update_cart_item(
    item_id: uuid.UUID, payload: schemas.UpdateCartItemRequest, identity: dict = Depends(get_cart_identity), db: AsyncSession = Depends(get_db)
):
    cart = await service.get_or_create_cart(db, identity["user_id"], identity["guest_token"])
    try:
        cart = await service.update_item_quantity(db, cart, item_id, payload.quantity)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return await _build_cart_response(db, cart)


@router.delete("/items/{item_id}", response_model=schemas.CartResponse)
async def remove_cart_item(item_id: uuid.UUID, identity: dict = Depends(get_cart_identity), db: AsyncSession = Depends(get_db)):
    cart = await service.get_or_create_cart(db, identity["user_id"], identity["guest_token"])
    try:
        cart = await service.remove_item(db, cart, item_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return await _build_cart_response(db, cart)