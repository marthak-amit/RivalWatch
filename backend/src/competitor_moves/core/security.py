"""Password hashing (scrypt, stdlib) and JWT issue/verify.

Tokens are HS256 JWTs whose jti is the id of a row in `sessions`. The token is only accepted while that
session is open (see services/sessions.py). Role and is_active are read from the DB on every request.
"""
import base64
import hashlib
import hmac
import secrets
import time
import uuid

import jwt

from ..config import get_settings

JWT_ALG = "HS256"
TTL = {"user": 7 * 24 * 3600, "admin": 12 * 3600}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):  # malformed stored hash (binascii.Error is a ValueError)
        return False


DUMMY_HASH = hash_password("dummy")  # unknown emails cost the same time as wrong passwords


def issue_token(user_id: int, role: str) -> tuple[str, dict]:
    """Returns (token, claims); the caller records claims["jti"] as a session."""
    now = int(time.time())
    claims = {"sub": str(user_id), "role": role, "jti": uuid.uuid4().hex, "iat": now, "exp": now + TTL[role]}
    return jwt.encode(claims, get_settings().jwt_secret, algorithm=JWT_ALG), claims


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError on a bad signature, expiry, or missing claims."""
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[JWT_ALG],
                      options={"require": ["exp", "sub", "jti"]})


def decode_expired_token(token: str) -> dict:
    """Signature-checked claims of a token that failed only on expiry, so its session can be closed as 'expired'."""
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[JWT_ALG],
                      options={"require": ["exp", "sub", "jti"], "verify_exp": False})
