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


def list_users(conn, idle_user_sec: int, idle_admin_sec: int) -> list[dict]:
    """Users plus session time: active_now, session_count, last_login_at, last/total session seconds."""
    return conn.execute("""
        select u.id, u.email, u.role, u.is_active, u.created_at,
               coalesce(s.active_now, false) as active_now,
               coalesce(s.session_count, 0) as session_count,
               s.last_login_at,
               s.last_session_seconds,
               coalesce(s.total_session_seconds, 0) as total_session_seconds
        from users u
        left join (
          select user_id,
                 count(*) as session_count,
                 max(started_at) as last_login_at,
                 (array_agg(extract(epoch from last_seen_at - started_at)::bigint order by started_at desc))[1]
                   as last_session_seconds,
                 sum(extract(epoch from last_seen_at - started_at))::bigint as total_session_seconds,
                 bool_or(ended_at is null and expires_at > now() and last_seen_at > now() - make_interval(
                   secs => case when role = 'admin' then %s else %s end)) as active_now
          from sessions group by user_id
        ) s on s.user_id = u.id
        order by u.id""", (idle_admin_sec, idle_user_sec)).fetchall()


def set_active(conn, user_id: int, is_active: bool) -> dict | None:
    """Only role='user' rows: admins can't be changed through the API."""
    return conn.execute(
        "update users set is_active=%s where id=%s and role='user' returning id,email,role,is_active",
        (is_active, user_id)).fetchone()


def delete_user(conn, user_id: int) -> bool:
    return conn.execute("delete from users where id=%s and role='user' returning id", (user_id,)).fetchone() is not None


def find_by_api_key(conn, api_key: str) -> dict | None:
    if not api_key:
        return None
    return conn.execute("select id,email,role,credits,plan from users where api_key=%s and is_active",
                        (api_key,)).fetchone()


def account(conn, user_id: int) -> dict:
    return conn.execute("select name,plan,credits,api_key,last_login_at from users where id=%s", (user_id,)).fetchone()
