import jwt
from fastapi import Depends, Header, HTTPException

from ..core.security import decode_token
from ..db.pool import get_conn
from ..db.repositories import users


def current_user(authorization: str = Header(None), conn=Depends(get_conn)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    try:
        claims = decode_token(authorization[7:])
        uid = int(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid or expired token") from None
    if users.is_revoked(conn, claims["jti"]):
        raise HTTPException(401, "Logged out")
    u = users.get_active_by_id(conn, uid)
    if not u or not u["is_active"]:
        raise HTTPException(401, "Account disabled")
    return {**u, "jti": claims["jti"], "exp": claims["exp"]}


def require_admin(u: dict = Depends(current_user)) -> dict:
    if u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return u
