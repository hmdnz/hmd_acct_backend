import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.delivery.models import Address


async def list_addresses(db: AsyncSession, user_id: uuid.UUID) -> list[Address]:
    result = await db.execute(select(Address).where(Address.user_id == user_id).order_by(Address.is_default.desc()))
    return result.scalars().all()


async def create_address(db: AsyncSession, user_id: uuid.UUID, data: dict) -> Address:
    existing = await list_addresses(db, user_id)
    is_first_address = len(existing) == 0

    if data.get("is_default") or is_first_address:
        await _unset_other_defaults(db, user_id)
        data["is_default"] = True

    address = Address(user_id=user_id, **data)
    db.add(address)
    await db.commit()
    await db.refresh(address)
    return address


async def update_address(db: AsyncSession, user_id: uuid.UUID, address_id: uuid.UUID, data: dict) -> Address:
    address = await _get_owned_address(db, user_id, address_id)

    if data.get("is_default"):
        await _unset_other_defaults(db, user_id)

    for key, value in data.items():
        setattr(address, key, value)

    await db.commit()
    await db.refresh(address)
    return address


async def delete_address(db: AsyncSession, user_id: uuid.UUID, address_id: uuid.UUID):
    address = await _get_owned_address(db, user_id, address_id)
    was_default = address.is_default

    await db.delete(address)
    await db.commit()

    if was_default:
        remaining = await list_addresses(db, user_id)
        if remaining:
            remaining[0].is_default = True
            await db.commit()

async def _get_owned_address(db: AsyncSession, user_id: uuid.UUID, address_id: uuid.UUID) -> Address:
    result = await db.execute(select(Address).where(Address.id == address_id, Address.user_id == user_id))
    address = result.scalars().first()
    if not address:
        raise ValueError("Address not found")
    return address


async def _unset_other_defaults(db: AsyncSession, user_id: uuid.UUID):
    result = await db.execute(select(Address).where(Address.user_id == user_id, Address.is_default == True))
    for addr in result.scalars().all():
        addr.is_default = False