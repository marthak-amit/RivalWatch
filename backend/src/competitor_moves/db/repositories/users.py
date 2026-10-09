import psycopg.errors


class EmailTaken(Exception):
    pass


def create_user(conn, email: str, password_hash: str) -> dict:
    try:
        return conn.execute(
            "insert into users(email,password_hash,role) values (%s,%s,'user') returning id,email,role",
            (email.strip().lower(), password_hash)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise EmailTaken(email) from None


def find_for_login(conn, email: str, role: str) -> dict | None:
    return conn.execute(
        "select id,email,role,password_hash,is_active from users where lower(email)=lower(%s) and role=%s",
        (email.strip(), role)).fetchone()


def get_active_by_id(conn, user_id: int) -> dict | None:
    return conn.execute("select id,email,role,is_active from users where id=%s", (user_id,)).fetchone()


def list_users(conn) -> list[dict]:
    return conn.execute("select id,email,role,is_active,created_at from users order by id").fetchall()


def set_active(conn, user_id: int, is_active: bool) -> dict | None:
    """Only role='user' rows: admins can't be changed through the API."""
    return conn.execute(
        "update users set is_active=%s where id=%s and role='user' returning id,email,role,is_active",
        (is_active, user_id)).fetchone()


def delete_user(conn, user_id: int) -> bool:
    return conn.execute("delete from users where id=%s and role='user' returning id", (user_id,)).fetchone() is not None


def is_revoked(conn, jti: str) -> bool:
    return conn.execute("select 1 from revoked_tokens where jti=%s", (jti,)).fetchone() is not None


def revoke(conn, jti: str, exp: int) -> None:
    conn.execute("delete from revoked_tokens where expires_at < now()")
    conn.execute("insert into revoked_tokens(jti, expires_at) values (%s, to_timestamp(%s)) on conflict do nothing",
                 (jti, exp))
