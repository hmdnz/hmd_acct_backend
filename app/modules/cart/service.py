import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.modules.cart.models import Cart, CartItem
from app.modules.catalog.models import Product
from app.modules.catalog.seller_check import unavailable_seller_ids


async def get_or_create_cart(db: AsyncSession, user_id, guest_token) -> Cart:
    if user_id:
        result = await db.execute(
            select(Cart).options(selectinload(Cart.items)).where(Cart.user_id == user_id)
        )
    else:
        result = await db.execute(
            select(Cart).options(selectinload(Cart.items)).where(Cart.guest_token == guest_token)
        )
    cart = result.scalars().first()

    if not cart:
        cart = Cart(user_id=user_id, guest_token=guest_token)
        db.add(cart)
        await db.commit()
        result = await db.execute(
            select(Cart).options(selectinload(Cart.items)).where(Cart.id == cart.id)
        )
        cart = result.scalars().one()
    return cart


async def add_item(db: AsyncSession, cart: Cart, product_id: uuid.UUID, quantity: int) -> Cart:
    product = await db.get(Product, product_id)
    if not product or not product.is_active or not product.is_approved:
        raise ValueError("Product not available")
    if await unavailable_seller_ids(db, [product.seller_id]):
        raise ValueError("Product not available")
    if quantity > product.stock_quantity:
        raise ValueError(f"Only {product.stock_quantity} in stock")

    existing = next((i for i in cart.items if i.product_id == product_id), None)
    if existing:
        existing.quantity += quantity
    else:
        db.add(CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity))

    await db.commit()
    return await get_or_create_cart(db, cart.user_id, cart.guest_token)


async def update_item_quantity(db: AsyncSession, cart: Cart, item_id: uuid.UUID, quantity: int) -> Cart:
    item = next((i for i in cart.items if i.id == item_id), None)
    if not item:
        raise ValueError("Item not in cart")

    if quantity <= 0:
        await db.delete(item)
    else:
        item.quantity = quantity
    await db.commit()
    return await get_or_create_cart(db, cart.user_id, cart.guest_token)


async def remove_item(db: AsyncSession, cart: Cart, item_id: uuid.UUID) -> Cart:
    item = next((i for i in cart.items if i.id == item_id), None)
    if not item:
        raise ValueError("Item not in cart")
    await db.delete(item)
    await db.commit()
    return await get_or_create_cart(db, cart.user_id, cart.guest_token)


async def merge_guest_cart_into_user(db: AsyncSession, guest_token: str | None, user_id: uuid.UUID):
    if not guest_token:
        return

    result = await db.execute(
        select(Cart).options(selectinload(Cart.items)).where(Cart.guest_token == guest_token)
    )
    guest_cart = result.scalars().first()
    if not guest_cart or not guest_cart.items:
        return

    user_cart = await get_or_create_cart(db, user_id, None)

    for guest_item in guest_cart.items:
        existing = next((i for i in user_cart.items if i.product_id == guest_item.product_id), None)
        if existing:
            existing.quantity += guest_item.quantity
        else:
            db.add(CartItem(cart_id=user_cart.id, product_id=guest_item.product_id, quantity=guest_item.quantity))

    await db.delete(guest_cart)
    await db.commit()