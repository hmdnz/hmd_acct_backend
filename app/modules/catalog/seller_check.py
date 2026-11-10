import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Seller


async def unavailable_seller_ids(db: AsyncSession, seller_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Of the given sellers, the ones who can't currently sell: missing, suspended
    (is_active False) or not approved. One query, however many sellers are passed."""
    ids = set(seller_ids)
    if not ids:
        return set()
    selling = set(
        (
            await db.execute(
                select(Seller.id).where(
                    Seller.id.in_(ids),
                    Seller.is_active.is_(True),
                    Seller.is_approved.is_(True),
                )
            )
        ).scalars().all()
    )
    return ids - selling
