from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
from app.db.session import get_db
from app.modules.auth.dependencies import get_current_customer
from app.modules.auth.models import User
from app.modules.delivery import schemas, service

router = APIRouter(prefix="/addresses", tags=["delivery"])


@router.get("", response_model=list[schemas.AddressResponse])
async def get_my_addresses(current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)):
    return await service.list_addresses(db, current_user.id)


@router.post("", response_model=schemas.AddressResponse)
async def add_address(
    payload: schemas.AddressCreateRequest, current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)
):
    return await service.create_address(db, current_user.id, payload.model_dump())


@router.patch("/{address_id}", response_model=schemas.AddressResponse)
async def edit_address(
    address_id: uuid.UUID, payload: schemas.AddressUpdateRequest,
    current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)
):
    try:
        return await service.update_address(db, current_user.id, address_id, payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/{address_id}")
async def remove_address(
    address_id: uuid.UUID, current_user: User = Depends(get_current_customer), db: AsyncSession = Depends(get_db)
):
    try:
        await service.delete_address(db, current_user.id, address_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"message": "Address deleted"}