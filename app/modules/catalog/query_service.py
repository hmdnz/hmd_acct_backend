# Full replacement for app/modules/catalog/query_service.py
# Changes are marked  # CHANGED  /  # NEW

from decimal import Decimal
from sqlalchemy import select, func, or_,case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.modules.catalog.models import Product, Category, Brand, Tag  # CHANGED: Tag added
from app.modules.auth.models import Seller
from app.modules.catalog.s3_service import get_public_url
from sqlalchemy import select, func, or_, case
from app.modules.orders.models import Order, OrderItem, OrderStatus, SellerOrder, SellerOrderStatus

def _search_doc():
    return func.to_tsvector(
        "english", func.coalesce(Product.name, "") + " " + func.coalesce(Product.description, "")
    )


def _search_words(query: str) -> list[str]:
    return list(dict.fromkeys(w for w in query.lower().split() if w))[:8]


def _apply_search(stmt, query: str):
    """Every word must match the product's text OR one of its tags."""
    doc = _search_doc()
    for word in _search_words(query):
        stmt = stmt.where(or_(
            doc.op("@@")(func.plainto_tsquery("english", word)),
            Product.tags.any(Tag.name == word),
        ))
    return stmt


async def search_products(
    db: AsyncSession,
    query: str | None = None,
    category_id=None,
    brand_id=None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    tag: str | None = None,  # NEW
    sort: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Product], int]:
    base = (
        select(Product)
        .join(Seller, Seller.id == Product.seller_id)
        .where(Product.is_active == True, Product.is_approved == True,
               Seller.is_active == True, Seller.is_approved == True)
    )
    if query:
        base = _apply_search(base, query)
    if category_id:
        base = base.where(Product.category_id == category_id)
    if brand_id:
        base = base.where(Product.brand_id == brand_id)
    if min_price is not None:
        base = base.where(Product.price >= min_price)
    if max_price is not None:
        base = base.where(Product.price <= max_price)
    if tag and tag.strip():  # NEW: tags are stored trimmed + lowercase, so normalize the same way
        base = base.where(Product.tags.any(Tag.name == tag.strip().lower()))

    count_result = await db.execute(select(func.count()).select_from(base.subquery()))
    total = count_result.scalar_one()

    # CHANGED: every sort ends with Product.id as a tiebreaker. Without it, products with
    # equal price/rating/timestamp can repeat or vanish between pages.
    if sort == "relevance" and not query:
        sort = "newest"
    elif sort is None:
        sort = "relevance" if query else "newest"

    if sort == "relevance":
        words = _search_words(query)
        rank = func.ts_rank(_search_doc(), func.plainto_tsquery("english", query))
        tag_boost = case((Product.tags.any(Tag.name.in_(words)), 0.2), else_=0.0)
        base = base.order_by((rank + tag_boost).desc(), Product.created_at.desc(), Product.id)
    elif sort == "best_selling":
        sold = (
            select(OrderItem.product_id.label("product_id"),
                   func.sum(OrderItem.quantity).label("units_sold"))
            .join(SellerOrder, OrderItem.seller_order_id == SellerOrder.id)
            .join(Order, SellerOrder.order_id == Order.id)
            .where(Order.status == OrderStatus.PAID,
                   SellerOrder.status != SellerOrderStatus.CANCELLED)
            .group_by(OrderItem.product_id)
            .subquery()
        )
        base = base.outerjoin(sold, sold.c.product_id == Product.id).order_by(
            func.coalesce(sold.c.units_sold, 0).desc(), Product.created_at.desc(), Product.id
        )
    elif sort == "price_asc":
        base = base.order_by(Product.price.asc(), Product.id)
    elif sort == "price_desc":
        base = base.order_by(Product.price.desc(), Product.id)
    elif sort == "rating":  # NEW
        base = base.order_by(
            Product.average_rating.desc(), Product.review_count.desc(),
            Product.created_at.desc(), Product.id,
        )
    else:
        base = base.order_by(Product.created_at.desc(), Product.id)

    base = base.options(selectinload(Product.images)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(base)
    return result.scalars().all(), total


async def get_product_detail(db: AsyncSession, product_id) -> Product | None:
    result = await db.execute(
        select(Product)
        .join(Seller, Seller.id == Product.seller_id)
        .options(selectinload(Product.images), selectinload(Product.tags))
        .where(Product.id == product_id, Product.is_active == True, Product.is_approved == True,
               Seller.is_active == True, Seller.is_approved == True)
    )
    return result.scalars().first()


async def list_categories(db: AsyncSession) -> list[Category]:
    result = await db.execute(select(Category).order_by(Category.name))
    return result.scalars().all()


async def list_brands(db: AsyncSession) -> list[Brand]:
    result = await db.execute(select(Brand).order_by(Brand.name))
    return result.scalars().all()


async def list_my_products(db: AsyncSession, seller_id) -> list[Product]:
    result = await db.execute(
        select(Product).options(selectinload(Product.images)).where(Product.seller_id == seller_id)
        .order_by(Product.created_at.desc())
    )
    return result.scalars().all()