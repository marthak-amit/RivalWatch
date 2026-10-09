from fastapi import APIRouter, Depends, HTTPException, Response

from ...db.pool import get_conn
from ...db.repositories import sessions as session_repo
from ...db.repositories import users
from ...services.sessions import idle_timeout_sec
from ..deps import require_admin
from ..schemas.auth import UserPatch

router = APIRouter(prefix="/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("")
def list_users(conn=Depends(get_conn)):
    return users.list_users(conn, idle_timeout_sec("user"), idle_timeout_sec("admin"))


@router.get("/{user_id}/sessions")
def user_sessions(user_id: int, conn=Depends(get_conn)):
    u = users.get_active_by_id(conn, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return session_repo.list_for_user(conn, user_id, idle_timeout_sec(u["role"]))


@router.patch("/{user_id}")
def patch_user(user_id: int, body: UserPatch, conn=Depends(get_conn)):
    row = users.set_active(conn, user_id, body.is_active)
    if not row:
        raise HTTPException(404, "User not found")  # admins can't be changed through the API
    if not body.is_active:
        session_repo.end_all_for_user(conn, user_id, "disabled")
    return row


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, conn=Depends(get_conn)):
    if not users.delete_user(conn, user_id):
        raise HTTPException(404, "User not found")
    return Response(status_code=204)
