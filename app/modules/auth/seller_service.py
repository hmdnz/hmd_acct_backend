from datetime import datetime, timedelta, timezone, date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.redis_client import redis_client
from app.core.security import generate_otp_code, hash_password, verify_password
from app.modules.auth.models import VerificationCode, VerificationChannel, VerificationPurpose, Seller, AccountType
from app.modules.auth.rate_limit import enforce_otp_rate_limit

CODE_TTL_MINUTES = 10
VERIFIED_FLAG_TTL_MINUTES = 30
MAX_ATTEMPTS = 5


def _verified_key(identifier: str, channel: VerificationChannel) -> str:
    return f"verified:{channel.value}:{identifier}"


async def request_phone_otp(db: AsyncSession, phone_number: str) -> str:
    existing = await db.execute(select(Seller).where(Seller.phone_number == phone_number))
    if existing.scalars().first():
        raise ValueError("Phone number already registered as a seller")

    await enforce_otp_rate_limit(phone_number, VerificationChannel.PHONE.value)
    code = generate_otp_code()
    db.add(VerificationCode(
        identifier=phone_number, channel=VerificationChannel.PHONE,
        purpose=VerificationPurpose.SELLER_REGISTRATION, hashed_code=hash_password(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES),
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return code


async def request_email_otp(db: AsyncSession, email: str) -> str:
    existing = await db.execute(select(Seller).where(Seller.email == email))
    if existing.scalars().first():
        raise ValueError("Email already registered as a seller")

    await enforce_otp_rate_limit(email, VerificationChannel.EMAIL.value)
    code = generate_otp_code()
    db.add(VerificationCode(
        identifier=email, channel=VerificationChannel.EMAIL,
        purpose=VerificationPurpose.SELLER_REGISTRATION, hashed_code=hash_password(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES),
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return code


async def _verify_code(db: AsyncSession, identifier: str, channel: VerificationChannel, code: str):
    result = await db.execute(
        select(VerificationCode)
        .where(
            VerificationCode.identifier == identifier,
            VerificationCode.channel == channel,
            VerificationCode.purpose == VerificationPurpose.SELLER_REGISTRATION,
            VerificationCode.is_used == False,
        )
        .order_by(VerificationCode.created_at.desc())
    )
    record = result.scalars().first()

    if not record or record.expires_at < datetime.now(timezone.utc):
        raise ValueError("Code expired or not found, please request a new one")
    if record.attempts >= MAX_ATTEMPTS:
        raise ValueError("Too many attempts, please request a new code")
    if not verify_password(code, record.hashed_code):
        record.attempts += 1
        await db.commit()
        raise ValueError("Incorrect code")

    record.is_used = True
    await db.commit()


async def verify_phone_otp(db: AsyncSession, phone_number: str, code: str):
    await _verify_code(db, phone_number, VerificationChannel.PHONE, code)
    await redis_client.setex(_verified_key(phone_number, VerificationChannel.PHONE), VERIFIED_FLAG_TTL_MINUTES * 60, "1")


async def verify_email_otp(db: AsyncSession, email: str, code: str):
    await _verify_code(db, email, VerificationChannel.EMAIL, code)
    await redis_client.setex(_verified_key(email, VerificationChannel.EMAIL), VERIFIED_FLAG_TTL_MINUTES * 60, "1")


async def complete_seller_registration(
    db: AsyncSession, first_name: str, last_name: str, date_of_birth: date,
    phone_number: str, email: str, password: str, account_type: AccountType,
    brand_name: str, location: str
) -> Seller:
    phone_verified = await redis_client.get(_verified_key(phone_number, VerificationChannel.PHONE))
    email_verified = await redis_client.get(_verified_key(email, VerificationChannel.EMAIL))

    if not phone_verified:
        raise ValueError("Please verify your phone number first")
    if not email_verified:
        raise ValueError("Please verify your email first")

    seller = Seller(
        first_name=first_name, last_name=last_name, date_of_birth=date_of_birth,
        phone_number=phone_number, email=email, hashed_password=hash_password(password),
        account_type=account_type, brand_name=brand_name, business_address=location,
        is_phone_verified=True, is_email_verified=True,
    )
    db.add(seller)
    await db.commit()

    await redis_client.delete(_verified_key(phone_number, VerificationChannel.PHONE))
    await redis_client.delete(_verified_key(email, VerificationChannel.EMAIL))
    return seller