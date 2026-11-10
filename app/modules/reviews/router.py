import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# ASSUMPTION: adjust these import paths to match your project.
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_customer
from app.modules.auth.models import User
from app.modules.reviews import service
from app.modules.reviews.models import Review
from app.modules.reviews.schemas import (
    RatingSummary,
    ReviewCreate,
    ReviewListOut,
    ReviewOut,
    ReviewSort,
    ReviewUpdate,
)

router = APIRouter(tags=["reviews"])


# ---------- public ----------

@router.get("/catalog/products/{product_id}/reviews", response_model=ReviewListOut)
async def list_product_reviews(
    product_id: uuid.UUID,
    sort: ReviewSort = "newest",
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_reviews(db, product_id, sort, limit, offset)


@router.get(
    "/catalog/products/{product_id}/reviews/summary", response_model=RatingSummary
)
async def product_rating_summary(
    product_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    return await service.get_summary(db, product_id)


# ---------- customer ----------

@router.post(
    "/catalog/products/{product_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_review(
    product_id: uuid.UUID,
    data: ReviewCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_customer),
):
    return await service.create_review(db, user, product_id, data)


@router.get("/catalog/products/{product_id}/reviews/mine", response_model=ReviewOut)
async def my_review_for_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_customer),
):
    """Lets the frontend decide whether to show 'Write a review' or 'Edit your review'."""
    review = (
        await db.execute(
            select(Review)
            .where(Review.product_id == product_id, Review.user_id == user.id)
            .options(selectinload(Review.user))
        )
    ).scalar_one_or_none()
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No review yet")
    return service.to_out(review, user.id)


@router.patch("/reviews/{review_id}", response_model=ReviewOut)
async def update_my_review(
    review_id: uuid.UUID,
    data: ReviewUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_customer),
):
    return await service.update_review(db, user, review_id, data)


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_review(
    review_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_customer),
):
    await service.delete_review(db, user, review_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
