import psycopg.errors


class EmailTaken(Exception):
    pass


def create_user(conn, email: str, password_hash: str, name: str | None = None, plan: str = "starter",
                credits: int = 300) -> dict:
    try:
        return conn.execute(
            "insert into users(email,password_hash,role,name,plan,credits) values (%s,%s,'user',%s,%s,%s) "
            "returning id,email,role,name,plan,credits",
            (email.strip().lower(), password_hash, name, plan, credits)).fetchone()
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
        select u.id, u.email, u.role, u.is_active, u.created_at, u.name, u.plan, u.credits, u.last_login_at,
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
    return conn.execute("select name,plan,credits,api_key,last_login_at,created_at from users where id=%s",
                        (user_id,)).fetchone()


UI_COLS = "id, email, name, role, plan, is_active, credits, api_key, created_at, last_login_at"


def ui_row(conn, user_id: int) -> dict | None:
    return conn.execute(f"select {UI_COLS} from users where id=%s", (user_id,)).fetchone()


def find_any_role(conn, email: str) -> dict | None:
    return conn.execute(f"select {UI_COLS}, password_hash from users where lower(email)=lower(%s)", (email.strip(),)).fetchone()


def create_ui_user(conn, email: str, name: str | None, password_hash: str, plan: str, credits: int) -> dict:
    try:
        return conn.execute(f"insert into users(email, name, password_hash, role, plan, credits) values (%s,%s,%s,'user',%s,%s) "
                            f"returning {UI_COLS}", (email.strip().lower(), name, password_hash, plan, credits)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise EmailTaken(email) from None


def update_fields(conn, user_id: int, **fields) -> dict | None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    return conn.execute(f"update users set {sets} where id=%s returning {UI_COLS}", [*fields.values(), user_id]).fetchone()


def list_ui(conn) -> list[dict]:
    return conn.execute(f"""select {UI_COLS},
        (select count(*) from sites s join projects p on p.id = s.project_id
          where p.owner_user_id = u.id and s.role = 'competitor' and s.status <> 'removed') as competitors
        from users u order by created_at desc""").fetchall()


def audit(conn, actor: dict, action: str, target: dict | None = None, detail: str = "") -> None:
    conn.execute("insert into audit_log(actor_id, actor_email, action, target_id, target_email, detail) values (%s,%s,%s,%s,%s,%s)",
                 (actor["id"], actor["email"], action, (target or {}).get("id"), (target or {}).get("email"), detail[:500]))


def audit_log(conn, limit: int = 100) -> list[dict]:
    return conn.execute("select ts, actor_email, action, target_email, detail from audit_log order by id desc limit %s",
                        (limit,)).fetchall()
