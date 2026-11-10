import uuid
from decimal import Decimal
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.modules.orders.models import Order, SellerOrder, OrderItem, OrderStatus
from app.modules.cart.models import Cart
from app.modules.catalog.models import Product
from app.modules.catalog.seller_check import unavailable_seller_ids
from app.modules.delivery.models import Address
from app.modules.auth.models import User


async def initiate_checkout(db: AsyncSession, user_id: uuid.UUID, cart: Cart, address_id: uuid.UUID) -> Order:
    if not cart.items:
        raise ValueError("Cart is empty")

    address = await db.get(Address, address_id)
    if not address or address.user_id != user_id:
        raise ValueError("Address not found")
    
    user = await db.get(User, user_id)
    if not user:
        raise ValueError("User not found")

    product_ids = [item.product_id for item in cart.items]
    result = await db.execute(select(Product).where(Product.id.in_(product_ids)))
    products_by_id = {p.id: p for p in result.scalars().all()}
    blocked_sellers = await unavailable_seller_ids(db, {p.seller_id for p in products_by_id.values()})

    items_by_seller = defaultdict(list)
    for cart_item in cart.items:
        product = products_by_id.get(cart_item.product_id)
        if (
            not product or not product.is_active or not product.is_approved
            or product.seller_id in blocked_sellers
        ):
            name = product.name if product else str(cart_item.product_id)
            raise ValueError(f"'{name}' is no longer available")
        if cart_item.quantity > product.stock_quantity:
            raise ValueError(f"'{product.name}' only has {product.stock_quantity} left in stock")
        items_by_seller[product.seller_id].append((product, cart_item.quantity))

    order = Order(
        user_id=user_id, address_id=address_id,
        total_amount=Decimal("0"), status=OrderStatus.PENDING_PAYMENT,
        ship_recipient_name=" ".join(filter(None, [user.first_name, user.last_name])),
        ship_phone_number=user.phone_number,
        ship_country=address.country, ship_state=address.state, ship_lga=address.lga,
        ship_street=address.street, ship_house_number=address.house_number,
        ship_landmark=address.landmark,
        ship_latitude=address.latitude, ship_longitude=address.longitude,
    )
    db.add(order)
    await db.flush()

    order_total = Decimal("0")
    for seller_id, product_quantities in items_by_seller.items():
        seller_subtotal = Decimal("0")
        seller_order = SellerOrder(order_id=order.id, seller_id=seller_id, subtotal=Decimal("0"))
        db.add(seller_order)
        await db.flush()

        for product, quantity in product_quantities:
            line_total = product.price * quantity
            seller_subtotal += line_total
            db.add(OrderItem(
                seller_order_id=seller_order.id, product_id=product.id,
                product_name=product.name, unit_price=product.price,
                quantity=quantity, line_total=line_total,
            ))
        seller_order.subtotal = seller_subtotal
        order_total += seller_subtotal

    order.total_amount = order_total
    await db.commit()

    result = await db.execute(
        select(Order).options(selectinload(Order.seller_orders).selectinload(SellerOrder.items))
        .where(Order.id == order.id)
    )
    return result.scalars().one()


async def get_order(db: AsyncSession, user_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    result = await db.execute(
        select(Order).options(selectinload(Order.seller_orders).selectinload(SellerOrder.items))
        .where(Order.id == order_id, Order.user_id == user_id)
    )
    order = result.scalars().first()
    if not order:
        raise ValueError("Order not found")
    return order


async def list_my_orders(db: AsyncSession, user_id: uuid.UUID) -> list[Order]:
    result = await db.execute(
        select(Order).options(selectinload(Order.seller_orders).selectinload(SellerOrder.items))
        .where(Order.user_id == user_id).order_by(Order.created_at.desc())
    )
    return result.scalars().all()