import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# ASSUMPTION: adjust these import paths/field names to match your actual files.
from app.modules.auth.models import User
from app.modules.catalog.models import Product
from app.modules.orders.models import Order, OrderItem, SellerOrder, SellerOrderStatus
from app.modules.reviews.models import Review
from app.modules.reviews.schemas import (
    RatingSummary,
    ReviewCreate,
    ReviewListOut,
    ReviewOut,
    ReviewSort,
    ReviewUpdate,
)


# ---------- helpers ----------

def display_name(user: User) -> str:
    """'Amina Bello' -> 'Amina B.' (don't expose full customer names publicly)."""
    last = (user.last_name or "").strip()
    initial = f" {last[0].upper()}." if last else ""
    return f"{user.first_name.strip()}{initial}"


def to_out(review: Review, current_user_id: uuid.UUID | None = None) -> ReviewOut:
    # review.user MUST have been eager-loaded (selectinload) by the caller.
    return ReviewOut(
        id=review.id,
        product_id=review.product_id,
        rating=review.rating,
        title=review.title,
        comment=review.comment,
        reviewer_name=display_name(review.user),
        is_own=current_user_id is not None and review.user_id == current_user_id,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


async def _get_reviewable_product(db: AsyncSession, product_id: uuid.UUID) -> Product:
    product = await db.get(Product, product_id)
    if not product or not product.is_active or not product.is_approved:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


async def has_purchased_delivered(
    db: AsyncSession, user_id: uuid.UUID, product_id: uuid.UUID
) -> bool:
    stmt = (
        select(OrderItem.id)
        .join(SellerOrder, OrderItem.seller_order_id == SellerOrder.id)
        .join(Order, SellerOrder.order_id == Order.id)
        .where(
            Order.user_id == user_id,
            OrderItem.product_id == product_id,
            SellerOrder.status == SellerOrderStatus.DELIVERED,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


async def refresh_product_rating(db: AsyncSession, product_id: uuid.UUID) -> None:
    """Recompute denormalized aggregates from visible reviews. Call inside the
    same transaction as the review write; the caller commits."""
    # Lock the product row so two concurrent review writes recompute one after the other.
    await db.execute(select(Product.id).where(Product.id == product_id).with_for_update())
    row = (
        await db.execute(
            select(func.coalesce(func.avg(Review.rating), 0), func.count(Review.id))
            .where(Review.product_id == product_id, Review.is_visible.is_(True))
        )
    ).one()
    avg = Decimal(str(row[0])).quantize(Decimal("0.01"))
    await db.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(average_rating=avg, review_count=row[1])
    )


# ---------- operations ----------

async def _fetch_review(db: AsyncSession, review_id: uuid.UUID) -> Review:
    return (
        await db.execute(
            select(Review).where(Review.id == review_id)
            .options(selectinload(Review.user))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

async def create_review(
    db: AsyncSession, user: User, product_id: uuid.UUID, data: ReviewCreate
) -> ReviewOut:
    await _get_reviewable_product(db, product_id)

    if not await has_purchased_delivered(db, user.id, product_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only review products you have purchased and received",
        )

    review = Review(
        product_id=product_id,
        user_id=user.id,
        rating=data.rating,
        title=data.title,
        comment=data.comment,
    )
    db.add(review)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You have already reviewed this product"
        )

    review_id = review.id
    user_id = user.id  # capture before commit; the commit expires `user`
    await refresh_product_rating(db, product_id)
    await db.commit()

    return to_out(await _fetch_review(db, review_id), user_id)


async def list_reviews(
    db: AsyncSession,
    product_id: uuid.UUID,
    sort: ReviewSort,
    limit: int,
    offset: int,
    current_user_id: uuid.UUID | None = None,
) -> ReviewListOut:
    await _get_reviewable_product(db, product_id)

    order_by = {
        "newest": [Review.created_at.desc()],
        "highest": [Review.rating.desc(), Review.created_at.desc()],
        "lowest": [Review.rating.asc(), Review.created_at.desc()],
    }[sort]

    base = (Review.product_id == product_id, Review.is_visible.is_(True))

    total = (
        await db.execute(select(func.count(Review.id)).where(*base))
    ).scalar_one()

    rows = (
        await db.execute(
            select(Review)
            .where(*base)
            .options(selectinload(Review.user))
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return ReviewListOut(
        items=[to_out(r, current_user_id) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_summary(db: AsyncSession, product_id: uuid.UUID) -> RatingSummary:
    product = await _get_reviewable_product(db, product_id)

    rows = (
        await db.execute(
            select(Review.rating, func.count(Review.id))
            .where(Review.product_id == product_id, Review.is_visible.is_(True))
            .group_by(Review.rating)
        )
    ).all()

    distribution = {str(i): 0 for i in range(5, 0, -1)}
    for rating, count in rows:
        distribution[str(rating)] = count

    return RatingSummary(
        average_rating=product.average_rating,
        review_count=product.review_count,
        distribution=distribution,
    )


async def _get_own_review(
    db: AsyncSession, user_id: uuid.UUID, review_id: uuid.UUID
) -> Review:
    # Check review_id AND user_id (same ownership pattern as addresses).
    review = (
        await db.execute(
            select(Review)
            .where(Review.id == review_id, Review.user_id == user_id)
            .options(selectinload(Review.user))
        )
    ).scalar_one_or_none()
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return review


async def update_review(
    db: AsyncSession, user: User, review_id: uuid.UUID, data: ReviewUpdate
) -> ReviewOut:
    user_id = user.id
    review = await _get_own_review(db, user_id, review_id)

    changes = data.model_dump(exclude_unset=True)
    if "rating" in changes and changes["rating"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "rating cannot be null")

    for field, value in changes.items():
        setattr(review, field, value)

    await db.flush()
    await refresh_product_rating(db, review.product_id)
    await db.commit()

    return to_out(await _fetch_review(db, review_id), user_id)


async def delete_review(db: AsyncSession, user: User, review_id: uuid.UUID) -> None:
    review = await _get_own_review(db, user.id, review_id)
    product_id = review.product_id
    await db.delete(review)
    await db.flush()
    await refresh_product_rating(db, product_id)
    await db.commit()
