"""Login sessions: start, validate (expiry + idle timeout), end. Each login is one row in `sessions`."""
from fastapi import Request

from ..config import get_settings
from ..core.security import issue_token
from ..db.repositories import sessions as repo


class SessionInvalid(Exception):
    """The token's session is closed, expired or idle. The message is safe to show the user."""


def idle_timeout_sec(role: str) -> int:
    s = get_settings()
    return 60 * (s.idle_timeout_admin_min if role == "admin" else s.idle_timeout_user_min)


def start(conn, user_id: int, role: str, request: Request | None) -> dict:
    token, claims = issue_token(user_id, role)
    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    row = repo.create(conn, claims["jti"], user_id, role, claims["exp"], ip, ua)
    conn.execute("update users set last_login_at=now() where id=%s", (user_id,))
    return {"token": token, "expires_at": row["expires_at"], "idle_timeout_sec": idle_timeout_sec(role)}


def validate(conn, jti: str, role: str) -> dict:
    """Returns timing info for an open session and records activity, or raises SessionInvalid."""
    row = repo.get(conn, jti)
    if not row or row["ended_at"]:
        raise SessionInvalid("Logged out" if not row or row["end_reason"] == "logout" else "Session ended")
    if row["expires_at"] <= row["db_now"]:
        repo.end(conn, jti, "expired")
        raise SessionInvalid("Session expired")
    idle = idle_timeout_sec(role)
    if (row["db_now"] - row["last_seen_at"]).total_seconds() > idle:
        repo.end(conn, jti, "idle")
        raise SessionInvalid("Session timed out after inactivity")
    repo.touch(conn, jti)
    return {"started_at": row["started_at"], "last_seen_at": row["last_seen_at"], "expires_at": row["expires_at"],
            "expires_in_sec": int((row["expires_at"] - row["db_now"]).total_seconds()),
            "idle_timeout_sec": idle}


def expire(conn, jti: str) -> None:
    repo.end(conn, jti, "expired")


def end(conn, jti: str, reason: str = "logout") -> None:
    repo.end(conn, jti, reason)
