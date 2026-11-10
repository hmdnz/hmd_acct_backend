import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

# ASSUMPTION: adjust these paths/names to match your project.
from app.db.session import get_db
from app.modules.cart.dependencies import get_cart_identity
from app.modules.wishlist import schemas, service

router = APIRouter(prefix="/wishlist", tags=["wishlist"])


def _owner(identity: dict) -> tuple[uuid.UUID | None, str | None]:
    """get_cart_identity returns {"user_id": ..., "guest_token": ...}."""
    return identity["user_id"], identity["guest_token"]


@router.get("", response_model=schemas.WishlistOut)
async def get_wishlist(
    db: AsyncSession = Depends(get_db), identity=Depends(get_cart_identity)
):
    user_id, guest_token = _owner(identity)
    return await service.list_items(db, user_id, guest_token)


@router.get("/product-ids", response_model=schemas.WishlistProductIds)
async def get_wishlisted_product_ids(
    db: AsyncSession = Depends(get_db), identity=Depends(get_cart_identity)
):
    user_id, guest_token = _owner(identity)
    return await service.list_product_ids(db, user_id, guest_token)


@router.post("/items", status_code=status.HTTP_204_NO_CONTENT)
async def add_to_wishlist(
    data: schemas.WishlistAdd,
    db: AsyncSession = Depends(get_db),
    identity=Depends(get_cart_identity),
):
    user_id, guest_token = _owner(identity)
    await service.add_item(db, user_id, guest_token, data.product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/items/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_wishlist(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    identity=Depends(get_cart_identity),
):
    user_id, guest_token = _owner(identity)
    await service.remove_item(db, user_id, guest_token, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)