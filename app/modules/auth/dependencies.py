from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app.core.security import decode_token
from app.db.session import get_db
from app.modules.auth.models import User, Seller, Admin

bearer_scheme = HTTPBearer()


async def _get_subject_with_context(token: str, db: AsyncSession, required_context: str, model):
    credentials_error = HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not validate credentials")
    try:
        payload = decode_token(token)
    except JWTError:
        raise credentials_error

    if payload.get("type") != "access" or payload.get("context") != required_context:
        raise credentials_error

    subject = await db.get(model, payload.get("sub"))
    if subject is None or not subject.is_active:
        raise credentials_error
    return subject


async def get_current_customer(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    return await _get_subject_with_context(credentials.credentials, db, "customer", User)


async def get_current_seller(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme), db: AsyncSession = Depends(get_db)
) -> Seller:
    return await _get_subject_with_context(credentials.credentials, db, "seller", Seller)

async def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme), db: AsyncSession = Depends(get_db)
) -> Admin:
    return await _get_subject_with_context(credentials.credentials, db, "admin", Admin)


# async def get_current_rider(
#     credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
#     db: AsyncSession = Depends(get_db)
# ) -> Rider:
#     return await _get_subject_with_context(
#         credentials.credentials,
#         db,
#         "rider",
#         Rider
#     )

