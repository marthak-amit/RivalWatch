from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...core.security import DUMMY_HASH, hash_password, verify_password
from ...db.pool import get_conn
from ...db.repositories import users
from ...services import sessions
from ..deps import current_user
from ..schemas.auth import Credentials, Login

router = APIRouter(prefix="/user", tags=["user"])


def login_as(conn, body: Login, role: str, request: Request) -> dict:
    """Shared by user and admin login; each only accepts its own role."""
    u = users.find_for_login(conn, body.email, role)
    ok = verify_password(body.password, u["password_hash"] if u else DUMMY_HASH)
    if not (u and ok and u["is_active"]):
        raise HTTPException(401, "Invalid credentials")
    return {**sessions.start(conn, u["id"], role, request), "role": role, "email": u["email"]}


def logout(conn, u: dict) -> Response:
    sessions.end(conn, u["jti"], "logout")
    return Response(status_code=204)


@router.post("/signup", status_code=201)
def signup(body: Credentials, request: Request, conn=Depends(get_conn)):
    try:
        row = users.create_user(conn, body.email, hash_password(body.password))
    except users.EmailTaken:
        raise HTTPException(409, "Email already registered") from None
    return {**row, **sessions.start(conn, row["id"], "user", request)}


@router.post("/login")
def user_login(body: Login, request: Request, conn=Depends(get_conn)):
    return login_as(conn, body, "user", request)


@router.post("/logout", status_code=204)
def user_logout(u=Depends(current_user), conn=Depends(get_conn)):
    return logout(conn, u)


@router.get("/me")
def me(u=Depends(current_user), conn=Depends(get_conn)):
    return {"id": u["id"], "email": u["email"], "role": u["role"], **users.account(conn, u["id"]),
            "session": u["session"]}
