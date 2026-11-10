from pydantic import BaseModel, Field, EmailStr


class CustomerRegisterRequest(BaseModel):
    first_name: str = Field(min_length=2, max_length=100)
    last_name: str = Field(min_length=2, max_length=100)
    phone_number: str
    password: str = Field(min_length=8, max_length=128)


class VerifyOTPRequest(BaseModel):
    phone_number: str
    code: str = Field(min_length=6, max_length=6)
    guest_token: str | None = None


from datetime import date as date_type
from app.modules.auth.models import AccountType

class PhoneOTPRequest(BaseModel):
    phone_number: str

class EmailOTPRequest(BaseModel):
    email: EmailStr

class VerifyPhoneOTPRequest(BaseModel):
    phone_number: str
    code: str = Field(min_length=6, max_length=6)

class VerifyEmailOTPRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)

class SellerCompleteRegistrationRequest(BaseModel):
    first_name: str = Field(min_length=2, max_length=100)
    last_name: str = Field(min_length=2, max_length=100)
    date_of_birth: date_type
    phone_number: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    account_type: AccountType
    brand_name: str = Field(min_length=2, max_length=150)
    location: str

# class SellerCompleteRegistrationRequest(BaseModel):
#     phone_number: str
#     password: str = Field(min_length=8)
#     account_type: AccountType
#     brand_name: str = Field(min_length=2, max_length=150)
#     location: str
    
class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class LoginRequest(BaseModel):
    phone_number: str
    password: str = Field(max_length=128)
    guest_token: str | None = None  # frontend sends this if the shopper had been browsing as a guest


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    