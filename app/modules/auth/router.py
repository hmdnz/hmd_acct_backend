from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import get_db
from app.core.security import create_access_token, create_refresh_token, verify_password
from app.modules.auth import schemas, service, seller_service
from app.modules.auth.otp_service import verify_otp_and_register
from app.modules.auth.dependencies import get_current_customer
from app.modules.auth.models import User, Seller
from app.core.config import settings
from app.modules.notifications.termii_service import send_sms
from app.modules.notifications.email_service import send_email
from app.modules.cart.service import merge_guest_cart_into_user
from app.modules.wishlist.service import merge_guest_wishlist_into_user
from app.modules.auth.rate_limit import (
    clear_login_failures, enforce_login_lockout, record_login_failure,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# @router.post("/customer/register")
# async def customer_register(payload: schemas.CustomerRegisterRequest, db: AsyncSession = Depends(get_db)):
#     existing = await db.execute(select(User).where(User.phone_number == payload.phone_number))
#     if existing.scalars().first():
#         raise HTTPException(400, "Phone number already registered")

#     await service.register_customer(db, payload.full_name, payload.phone_number, payload.password)
#     return {"message": "OTP sent to your phone"}

async def _dispatch_otp_sms(phone_number: str, code: str):
    message = f"Your verification code is {code}. It expires in 10 minutes. Do not share this code."
    await send_sms(phone_number, message)

@router.post("/customer/register")
async def customer_register(
    payload: schemas.CustomerRegisterRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
):
    existing = await db.execute(select(User).where(User.phone_number == payload.phone_number))
    if existing.scalars().first():
        raise HTTPException(400, "Phone number already registered")

    try:
        code = await service.register_customer(db, payload.first_name, payload.last_name, payload.phone_number, payload.password)
    except ValueError as e:
        raise HTTPException(429, str(e))

    background_tasks.add_task(_dispatch_otp_sms, payload.phone_number, code)

    response = {"message": "OTP sent to your phone"}
    if settings.ENVIRONMENT == "development":
        response["dev_otp_code"] = code
    return response


@router.post("/customer/verify-otp", response_model=schemas.TokenResponse)
async def customer_verify_otp(payload: schemas.VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await verify_otp_and_register(db, payload.phone_number, payload.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    
    await merge_guest_cart_into_user(db, payload.guest_token, user.id)       # skip if verify_otp_and_register already does this
    await merge_guest_wishlist_into_user(db, payload.guest_token, user.id)

    return schemas.TokenResponse(
        access_token=create_access_token(str(user.id), context="customer"),
        refresh_token=create_refresh_token(str(user.id), context="customer"),
    )



@router.post("/customer/login", response_model=schemas.TokenResponse)
async def customer_login(payload: schemas.LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    phone = payload.phone_number.strip()
    ip = _client_ip(request)
    try:
        await enforce_login_lockout("customer", phone, ip)
    except ValueError as e:
        raise HTTPException(429, str(e))

    result = await db.execute(select(User).where(User.phone_number == payload.phone_number))
    user = result.scalars().first()

    if not user or not verify_password(payload.password, user.hashed_password):
        await record_login_failure("customer", phone, ip)
        raise HTTPException(401, "Invalid phone number or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")

    await clear_login_failures("customer", phone)
    await merge_guest_cart_into_user(db, payload.guest_token, user.id)
    await merge_guest_wishlist_into_user(db, payload.guest_token, user.id)
    return schemas.TokenResponse(
        access_token=create_access_token(str(user.id), context="customer"),
        refresh_token=create_refresh_token(str(user.id), context="customer"),
    )


async def _dispatch_email_verification(email: str, code: str):
    body = f"Your verification code is {code}. It expires in 10 minutes."
    await send_email(email, "Verify your email", body)


# @router.post("/seller/register")
# async def seller_register(
#     payload: schemas.SellerRegisterRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
# ):
#     if (await db.execute(select(Seller).where(Seller.phone_number == payload.phone_number))).scalars().first():
#         raise HTTPException(400, "Phone number already registered as a seller")
#     if (await db.execute(select(Seller).where(Seller.email == payload.email))).scalars().first():
#         raise HTTPException(400, "Email already registered as a seller")

#     code = await seller_service.initiate_seller_registration(
#         db, payload.contact_name, payload.phone_number, payload.email,
#         payload.brand_name, payload.business_address, payload.seller_password
#     )
#     background_tasks.add_task(_dispatch_otp_sms, payload.phone_number, code)

#     response = {"message": "OTP sent to your phone"}
#     if settings.ENVIRONMENT == "development":
#         response["dev_otp_code"] = code
#     return response


# @router.post("/seller/verify-phone-otp")
# async def seller_verify_phone_otp(
#     payload: schemas.VerifyOTPRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
# ):
#     try:
#         seller, email_code = await seller_service.verify_seller_phone_otp(db, payload.phone_number, payload.code)
#     except ValueError as e:
#         raise HTTPException(400, str(e))

#     background_tasks.add_task(_dispatch_email_verification, seller.email, email_code)

#     response = {"message": "Phone verified. A code was sent to your email."}
#     if settings.ENVIRONMENT == "development":
#         response["dev_email_code"] = email_code
#     return response


# @router.post("/seller/verify-email", response_model=schemas.TokenResponse)
# async def seller_verify_email(payload: schemas.VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
#     try:
#         seller = await seller_service.verify_seller_email(db, payload.email, payload.code)
#     except ValueError as e:
#         raise HTTPException(400, str(e))

#     return schemas.TokenResponse(
#         access_token=create_access_token(str(seller.id), context="seller"),
#         refresh_token=create_refresh_token(str(seller.id), context="seller"),
#     )


# @router.post("/seller/login", response_model=schemas.TokenResponse)
# async def seller_login(payload: schemas.LoginRequest, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(Seller).where(Seller.phone_number == payload.phone_number))
#     seller = result.scalars().first()

#     if not seller or not verify_password(payload.password, seller.hashed_password):
#         raise HTTPException(401, "Invalid phone number or password")
#     if not seller.is_phone_verified or not seller.is_email_verified:
#         raise HTTPException(403, "Please complete phone and email verification first")
#     if not seller.is_active:
#         raise HTTPException(403, "Seller account disabled")

#     return schemas.TokenResponse(
#         access_token=create_access_token(str(seller.id), context="seller"),
#         refresh_token=create_refresh_token(str(seller.id), context="seller"),
    # )
    
# async def _dispatch_email_verification(email: str, code: str, first_name: str = ""):
#     greeting = f"Hi {first_name}," if first_name else "Hi,"
#     body = f"{greeting}\n\nYour verification code is {code}. It expires in 10 minutes."
#     await send_email(email, "Verify your email", body)

async def _dispatch_email_verification(email: str, code: str):
    body = f"Your verification code is {code}. It expires in 10 minutes."
    await send_email(email, "Verify your email", body)

@router.post("/seller/login", response_model=schemas.TokenResponse)
async def seller_login(payload: schemas.LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    phone = payload.phone_number.strip()
    ip = _client_ip(request)
    try:
        await enforce_login_lockout("seller", phone, ip)
    except ValueError as e:
        raise HTTPException(429, str(e))

    result = await db.execute(select(Seller).where(Seller.phone_number == payload.phone_number))
    seller = result.scalars().first()

    if not seller or not verify_password(payload.password, seller.hashed_password):
        await record_login_failure("seller", phone, ip)
        raise HTTPException(401, "Invalid phone number or password")
    if not seller.is_phone_verified or not seller.is_email_verified:
        raise HTTPException(403, "Please complete phone and email verification first")
    if not seller.is_active:
        raise HTTPException(403, "Seller account disabled")

    await clear_login_failures("seller", phone)
    return schemas.TokenResponse(
        access_token=create_access_token(str(seller.id), context="seller"),
        refresh_token=create_refresh_token(str(seller.id), context="seller"),
    )


@router.post("/seller/phone/request-otp")
async def seller_request_phone_otp(
    payload: schemas.PhoneOTPRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
):
    try:
        code = await seller_service.request_phone_otp(db, payload.phone_number)
    except ValueError as e:
        raise HTTPException(400, str(e))

    background_tasks.add_task(_dispatch_otp_sms, payload.phone_number, code)
    response = {"message": "OTP sent to your phone"}
    if settings.ENVIRONMENT == "development":
        response["dev_otp_code"] = code
    return response


@router.post("/seller/phone/verify-otp")
async def seller_verify_phone_otp(payload: schemas.VerifyPhoneOTPRequest, db: AsyncSession = Depends(get_db)):
    try:
        await seller_service.verify_phone_otp(db, payload.phone_number, payload.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "Phone verified"}


@router.post("/seller/email/request-otp")
async def seller_request_email_otp(
    payload: schemas.EmailOTPRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
):
    try:
        code = await seller_service.request_email_otp(db, payload.email)
    except ValueError as e:
        raise HTTPException(400, str(e))

    background_tasks.add_task(_dispatch_email_verification, payload.email, code)
    response = {"message": "Code sent to your email"}
    if settings.ENVIRONMENT == "development":
        response["dev_email_code"] = code
    return response


@router.post("/seller/email/verify-otp")
async def seller_verify_email_otp(payload: schemas.VerifyEmailOTPRequest, db: AsyncSession = Depends(get_db)):
    try:
        await seller_service.verify_email_otp(db, payload.email, payload.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"message": "Email verified"}


@router.post("/seller/complete-registration", response_model=schemas.TokenResponse)
async def seller_complete_registration(payload: schemas.SellerCompleteRegistrationRequest, db: AsyncSession = Depends(get_db)):
    try:
        seller = await seller_service.complete_seller_registration(
            db, payload.first_name, payload.last_name, payload.date_of_birth,
            payload.phone_number, payload.email, payload.password,
            payload.account_type, payload.brand_name, payload.location
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return schemas.TokenResponse(
        access_token=create_access_token(str(seller.id), context="seller"),
        refresh_token=create_refresh_token(str(seller.id), context="seller"),
    )