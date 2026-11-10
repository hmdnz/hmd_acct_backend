from app.core.security import hash_password
from app.modules.auth.otp_service import initiate_registration_otp
from sqlalchemy.ext.asyncio import AsyncSession


async def register_customer(db: AsyncSession, first_name: str, last_name: str, phone_number: str, password: str) -> str:
    pending_data = {
        "first_name": first_name,
        "last_name": last_name,
        "hashed_password": hash_password(password),
    }
    return await initiate_registration_otp(db, phone_number, pending_data)