import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.modules.auth.models import Admin

bearer_scheme = HTTPBearer()


async def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme), db: AsyncSession = Depends(get_db)
) -> Admin:
    unauthorised = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    try:
        payload = decode_token(credentials.credentials)
        if payload.get("type") != "access" or payload.get("context") != "admin":
            raise unauthorised
        admin = await db.get(Admin, uuid.UUID(payload["sub"]))
    except (JWTError, KeyError, ValueError):
        raise unauthorised
    if not admin or not admin.is_active:
        raise unauthorised
    return admin
