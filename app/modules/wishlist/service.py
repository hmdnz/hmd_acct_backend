import uuid

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# ASSUMPTION: adjust import paths to match your project.
from app.modules.catalog.models import Product
from app.modules.catalog.seller_check import unavailable_seller_ids
from app.modules.wishlist import schemas
from app.modules.wishlist.models import Wishlist, WishlistItem

MAX_ITEMS = 200


# ---------- internal helpers ----------

async def _get_wishlist(
    db: AsyncSession, user_id: uuid.UUID | None, guest_token: str | None
) -> Wishlist | None:
    if not user_id and not guest_token:
        return None  # never query with both empty: guest_token IS NULL matches user wishlists

    stmt = select(Wishlist)
    if user_id:
        stmt = stmt.where(Wishlist.user_id == user_id)
    else:
        stmt = stmt.where(Wishlist.guest_token == guest_token)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_or_create_wishlist(
    db: AsyncSession, user_id: uuid.UUID | None, guest_token: str | None
) -> Wishlist:
    wishlist = await _get_wishlist(db, user_id, guest_token)
    if wishlist:
        return wishlist

    wishlist = Wishlist(
        user_id=user_id, guest_token=None if user_id else guest_token
    )
    db.add(wishlist)
    try:
        await db.flush()
    except IntegrityError:
        # Two simultaneous first-adds: the other request won, so use theirs.
        await db.rollback()
        wishlist = await _get_wishlist(db, user_id, guest_token)
        if wishlist is None:
            raise
    return wishlist


def _to_item_out(
    item: WishlistItem, blocked_sellers: set[uuid.UUID] | frozenset = frozenset()
) -> schemas.WishlistItemOut:
    p = item.product  # eager-loaded by caller
    urls = p.image_urls
    return schemas.WishlistItemOut(
        product_id=p.id,
        name=p.name,
        price=p.price,
        compare_at_price=p.compare_at_price,
        average_rating=p.average_rating,
        review_count=p.review_count,
        image_url=urls[0] if urls else None,
        in_stock=p.stock_quantity > 0,
        is_available=p.is_active and p.is_approved and p.seller_id not in blocked_sellers,
        added_at=item.created_at,
    )


# ---------- operations ----------

async def list_items(
    db: AsyncSession, user_id: uuid.UUID | None, guest_token: str | None
) -> schemas.WishlistOut:
    wishlist = await _get_wishlist(db, user_id, guest_token)
    if not wishlist:
        return schemas.WishlistOut(items=[], total=0)

    rows = (
        await db.execute(
            select(WishlistItem)
            .where(WishlistItem.wishlist_id == wishlist.id)
            .options(selectinload(WishlistItem.product).selectinload(Product.images))
            .order_by(WishlistItem.created_at.desc())
        )
    ).scalars().all()

    blocked = await unavailable_seller_ids(db, {r.product.seller_id for r in rows})
    items = [_to_item_out(r, blocked) for r in rows]
    return schemas.WishlistOut(items=items, total=len(items))


async def list_product_ids(
    db: AsyncSession, user_id: uuid.UUID | None, guest_token: str | None
) -> schemas.WishlistProductIds:
    wishlist = await _get_wishlist(db, user_id, guest_token)
    if not wishlist:
        return schemas.WishlistProductIds(product_ids=[])

    ids = (
        await db.execute(
            select(WishlistItem.product_id).where(
                WishlistItem.wishlist_id == wishlist.id
            )
        )
    ).scalars().all()
    return schemas.WishlistProductIds(product_ids=list(ids))


async def add_item(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    guest_token: str | None,
    product_id: uuid.UUID,
) -> None:
    product = await db.get(Product, product_id)
    if not product or not product.is_active or not product.is_approved:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    if await unavailable_seller_ids(db, [product.seller_id]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")

    wishlist = await _get_or_create_wishlist(db, user_id, guest_token)

    count = (
        await db.execute(
            select(func.count(WishlistItem.id)).where(
                WishlistItem.wishlist_id == wishlist.id
            )
        )
    ).scalar_one()

    already = (
        await db.execute(
            select(WishlistItem.id).where(
                WishlistItem.wishlist_id == wishlist.id,
                WishlistItem.product_id == product_id,
            )
        )
    ).first()
    if already:
        return  # idempotent

    if count >= MAX_ITEMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Wishlist is full (max {MAX_ITEMS} items)",
        )

    # ON CONFLICT DO NOTHING covers the double-tap race without raising.
    await db.execute(
        pg_insert(WishlistItem)
        .values(wishlist_id=wishlist.id, product_id=product_id)
        .on_conflict_do_nothing(index_elements=["wishlist_id", "product_id"])
    )
    await db.commit()


async def remove_item(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    guest_token: str | None,
    product_id: uuid.UUID,
) -> None:
    wishlist = await _get_wishlist(db, user_id, guest_token)
    if not wishlist:
        return  # idempotent
    await db.execute(
        delete(WishlistItem).where(
            WishlistItem.wishlist_id == wishlist.id,
            WishlistItem.product_id == product_id,
        )
    )
    await db.commit()


async def merge_guest_wishlist_into_user(
    db: AsyncSession, guest_token: str | None, user_id: uuid.UUID
) -> None:
    """Call next to merge_guest_cart_into_user (login + registration completion).
    Safe to call with guest_token=None (no-op). Commits when it merges something."""
    if not guest_token:
        return

    guest = await _get_wishlist(db, None, guest_token)
    if not guest:
        return

    user_wl = await _get_or_create_wishlist(db, user_id, None)

    existing = set(
        (
            await db.execute(
                select(WishlistItem.product_id).where(
                    WishlistItem.wishlist_id == user_wl.id
                )
            )
        ).scalars().all()
    )
    guest_product_ids = (
        await db.execute(
            select(WishlistItem.product_id).where(
                WishlistItem.wishlist_id == guest.id
            )
        )
    ).scalars().all()

    room = MAX_ITEMS - len(existing)
    for pid in guest_product_ids:
        if pid in existing:
            continue
        if room <= 0:
            break
        db.add(WishlistItem(wishlist_id=user_wl.id, product_id=pid))
        room -= 1

    await db.flush()
    await db.delete(guest)  # cascades to its (now redundant) items
    await db.commit()