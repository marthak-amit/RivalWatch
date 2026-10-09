def create(conn, jti: str, user_id: int, role: str, exp: int, ip: str | None, user_agent: str | None) -> dict:
    return conn.execute(
        """insert into sessions(jti,user_id,role,expires_at,ip,user_agent)
           values (%s,%s,%s,to_timestamp(%s),%s,%s) returning started_at, expires_at""",
        (jti, user_id, role, exp, ip, (user_agent or "")[:200] or None)).fetchone()


def get(conn, jti: str) -> dict | None:
    return conn.execute(
        """select jti, user_id, role, started_at, last_seen_at, expires_at, ended_at, end_reason,
                  now() as db_now from sessions where jti=%s""", (jti,)).fetchone()


def touch(conn, jti: str, every_sec: int = 60) -> None:
    """Record activity, at most one write per `every_sec` per session."""
    conn.execute("""update sessions set last_seen_at=now()
                    where jti=%s and ended_at is null and last_seen_at < now() - make_interval(secs => %s)""",
                 (jti, every_sec))


def end(conn, jti: str, reason: str) -> None:
    conn.execute("update sessions set ended_at=now(), end_reason=%s where jti=%s and ended_at is null",
                 (reason, jti))


def end_all_for_user(conn, user_id: int, reason: str) -> None:
    conn.execute("update sessions set ended_at=now(), end_reason=%s where user_id=%s and ended_at is null",
                 (reason, user_id))


def list_for_user(conn, user_id: int, idle_sec: int, limit: int = 50) -> list[dict]:
    return conn.execute(
        """select started_at, last_seen_at, ended_at, end_reason, ip, user_agent,
                  extract(epoch from last_seen_at - started_at)::bigint as duration_sec,
                  (ended_at is null and expires_at > now()
                   and last_seen_at > now() - make_interval(secs => %s)) as active
           from sessions where user_id=%s order by started_at desc limit %s""",
        (idle_sec, user_id, limit)).fetchall()
