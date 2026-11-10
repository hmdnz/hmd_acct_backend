"""Celery jobs for payments. Registered in app/core/celery_app.py (include + beat_schedule).

Each run builds its own short-lived database engine: Celery tasks call asyncio.run(), which makes a new
event loop every time, and a pooled connection from an earlier loop would break.
"""
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import settings
from app.modules.payments import service


async def _with_session(work):
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)() as db:
            return await work(db)
    finally:
        await engine.dispose()


@celery_app.task(name="payments.release_eligible_seller_funds")
def release_eligible_seller_funds() -> dict:
    """Every 10 minutes: release earnings whose hold has passed, send each order's payout to the seller's bank
    (automatic mode), settle Squad transfers, and flag payouts that are stuck."""
    return asyncio.run(_with_session(service.run_seller_payout_cycle))


@celery_app.task(name="payments.create_daily_rider_payouts")
def create_daily_rider_payouts() -> int:
    """Once a day: bundle each rider's unpaid delivery earnings into a payout (sent automatically if RIDER_PAYOUT_AUTO)."""
    async def work(db):
        return len(await service.create_daily_rider_payouts(db))
    return asyncio.run(_with_session(work))
