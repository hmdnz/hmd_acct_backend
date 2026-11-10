import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.redis_client import redis_client
from app.core.security import generate_otp_code, hash_password, verify_password
from app.modules.auth.models import VerificationCode, VerificationChannel, VerificationPurpose, User
from app.modules.auth.rate_limit import enforce_otp_rate_limit

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5


async def initiate_registration_otp(db: AsyncSession, phone_number: str, pending_data: dict) -> str:
    await enforce_otp_rate_limit(phone_number, VerificationChannel.PHONE.value)

    code = generate_otp_code()
    verification = VerificationCode(
        identifier=phone_number,
        channel=VerificationChannel.PHONE,
        purpose=VerificationPurpose.CUSTOMER_REGISTRATION,
        hashed_code=hash_password(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES),
        created_at=datetime.now(timezone.utc),
    )
    db.add(verification)
    await db.commit()

    await redis_client.setex(f"pending_reg:{phone_number}", CODE_TTL_MINUTES * 60, json.dumps(pending_data))
    return code


async def verify_otp_and_register(db: AsyncSession, phone_number: str, code: str) -> User:
    result = await db.execute(
        select(VerificationCode)
        .where(
            VerificationCode.identifier == phone_number,
            VerificationCode.channel == VerificationChannel.PHONE,
            VerificationCode.purpose == VerificationPurpose.CUSTOMER_REGISTRATION,
            VerificationCode.is_used == False,
        )
        .order_by(VerificationCode.created_at.desc())
    )
    otp = result.scalars().first()

    if not otp or otp.expires_at < datetime.now(timezone.utc):
        raise ValueError("OTP expired or not found, please request a new one")
    if otp.attempts >= MAX_ATTEMPTS:
        raise ValueError("Too many attempts, please request a new OTP")
    if not verify_password(code, otp.hashed_code):
        otp.attempts += 1
        await db.commit()
        raise ValueError("Incorrect code")

    otp.is_used = True
    pending_raw = await redis_client.get(f"pending_reg:{phone_number}")
    if not pending_raw:
        raise ValueError("Registration session expired, please start again")
    data = json.loads(pending_raw)

    user = User(
        first_name=data["first_name"],
        last_name=data["last_name"],
        phone_number=phone_number,
        hashed_password=data["hashed_password"],
        is_phone_verified=True,
    )
    db.add(user)
    await db.commit()
    await redis_client.delete(f"pending_reg:{phone_number}")
    return user