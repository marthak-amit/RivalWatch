import jwt
from fastapi import Depends, Header, HTTPException

from ..core.security import decode_expired_token, decode_token
from ..db.pool import get_conn
from ..db.repositories import users
from ..services import sessions


def current_user(authorization: str = Header(None), conn=Depends(get_conn)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    return authenticate(conn, authorization[7:])


def authenticate(conn, token: str) -> dict:
    """The user behind a session token (bearer header or the UI's cookie). Raises HTTPException(401)."""
    try:
        claims = decode_token(token)
        uid = int(claims["sub"])
    except jwt.ExpiredSignatureError:
        try:
            sessions.expire(conn, decode_expired_token(token)["jti"])  # record why the session ended
        except jwt.PyJWTError:
            pass
        raise HTTPException(401, "Session expired") from None
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid or expired token") from None
    u = users.get_active_by_id(conn, uid)
    if not u or not u["is_active"]:
        raise HTTPException(401, "Account disabled")
    try:
        session = sessions.validate(conn, claims["jti"], u["role"])
    except sessions.SessionInvalid as e:
        raise HTTPException(401, str(e)) from None
    return {**u, "jti": claims["jti"], "session": session}


def require_admin(u: dict = Depends(current_user)) -> dict:
    if u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return u
