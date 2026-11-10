import base64
import hashlib
from cryptography.fernet import Fernet
from app.core.config import settings

def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)

def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()

def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
