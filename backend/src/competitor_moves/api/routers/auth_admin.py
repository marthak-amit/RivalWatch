from fastapi import APIRouter, Depends

from ...db.pool import get_conn
from ..deps import require_admin
from ..schemas.auth import Login
from .auth_user import login_as, logout

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/login")
def admin_login(body: Login, conn=Depends(get_conn)):
    return login_as(conn, body, "admin")


@router.post("/logout", status_code=204)
def admin_logout(u=Depends(require_admin), conn=Depends(get_conn)):
    return logout(conn, u)
