from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from jose import JWTError
from app.core.security import decode_token
from app.db.session import get_db
from app.modules.auth.models import User

optional_bearer = HTTPBearer(auto_error=False)  # doesn't reject if no token is sent


async def get_cart_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
    x_guest_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if credentials:
        try:
            payload = decode_token(credentials.credentials)
            if payload.get("type") == "access" and payload.get("context") == "customer":
                user = await db.get(User, payload.get("sub"))
                if user and user.is_active:
                    return {"user_id": user.id, "guest_token": None}
        except JWTError:
            pass  # invalid/expired token falls through to guest handling below

    if not x_guest_token:
        raise HTTPException(400, "Send an X-Guest-Token header, or log in, to use the cart.")
    if len(x_guest_token) > 64:
        raise HTTPException(400, "Invalid X-Guest-Token.")
    return {"user_id": None, "guest_token": x_guest_token}