"""Admin login for the accounting system. Paths are unchanged (/admin/auth/login, /admin/me), so the frontend keeps working."""
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_refresh_token, hash_password, verify_password
from app.db.session import get_db
from app.modules.auth import schemas
from app.modules.auth.dependencies import get_current_admin
from app.modules.auth.models import Admin

router = APIRouter(prefix="/admin", tags=["admin-auth"])

# Simple brute-force guard, kept in memory (fine for one instance): 5 wrong passwords per email+IP in 15 minutes.
_MAX_FAILURES, _WINDOW_SECONDS = 5, 15 * 60
_failures: dict[str, list[float]] = defaultdict(list)
_DUMMY_HASH = hash_password("not-a-real-password")  # keeps response time the same when the email does not exist


def _recent_failures(key: str) -> list[float]:
    cutoff = time.time() - _WINDOW_SECONDS
    _failures[key] = [t for t in _failures[key] if t > cutoff]
    return _failures[key]


@router.post("/auth/login", response_model=schemas.TokenResponse)
async def admin_login(payload: schemas.AdminLoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    email = payload.email.strip().lower()
    key = f"{email}|{request.client.host if request.client else 'unknown'}"
    if len(_recent_failures(key)) >= _MAX_FAILURES:
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")

    admin = (await db.execute(select(Admin).where(Admin.email == email))).scalars().first()
    if not admin:
        verify_password(payload.password, _DUMMY_HASH)
    if not admin or not verify_password(payload.password, admin.hashed_password):
        _failures[key].append(time.time())
        raise HTTPException(401, "Invalid email or password")
    if not admin.is_active:
        raise HTTPException(403, "Account disabled")

    _failures.pop(key, None)
    subject = str(admin.id)
    return schemas.TokenResponse(
        access_token=create_access_token(subject, context="admin"),
        refresh_token=create_refresh_token(subject, context="admin"),
    )


@router.get("/me", response_model=schemas.AdminMe)
async def admin_me(admin: Admin = Depends(get_current_admin)):
    return schemas.AdminMe(id=admin.id, first_name=admin.first_name, last_name=admin.last_name, email=admin.email)
