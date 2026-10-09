"""Secrets stored in the database (store API tokens) are encrypted with APP_ENCRYPTION_KEY (Fernet)."""
from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class CryptoError(ValueError):
    pass


def _fernet() -> Fernet:
    key = get_settings().app_encryption_key
    if not key:
        raise CryptoError("APP_ENCRYPTION_KEY is not set, so tokens can't be stored")
    try:
        return Fernet(key.encode())
    except ValueError:
        raise CryptoError("APP_ENCRYPTION_KEY is not a valid Fernet key") from None


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise CryptoError("stored token can't be decrypted (APP_ENCRYPTION_KEY changed?)") from None
