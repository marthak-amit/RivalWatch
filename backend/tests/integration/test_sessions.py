"""Login sessions: each login is a row with start, last activity, end time and end reason."""
from competitor_moves.db.pool import get_pool


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def sql(q, *a):
    with get_pool().connection() as c:
        return c.execute(q, a).fetchall()


def login_user(client, email="s@test.local"):
    """Signup logs the user in, so this leaves exactly one session."""
    r = client.post("/user/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201
    return r.json()


def test_signup_and_login_each_start_a_session(client):
    login_user(client)
    r = client.post("/user/login", json={"email": "s@test.local", "password": "password123"},
                    headers={"user-agent": "pytest-agent"})
    assert r.status_code == 200
    body = r.json()
    assert sql("select count(*) as n from sessions where user_id=(select id from users where email='s@test.local')"
               ) == [{"n": 2}]
    assert body["idle_timeout_sec"] == 480 * 60 and body["expires_at"]
    me = client.get("/user/me", headers=bearer(body["token"])).json()
    s = me["session"]
    assert 0 < s["expires_in_sec"] <= 7 * 24 * 3600
    assert s["idle_timeout_sec"] == 480 * 60 and s["started_at"] and s["last_seen_at"]
    row = sql("select s.role, s.user_agent, s.ended_at from sessions s join users u on u.id=s.user_id "
              "where u.email='s@test.local' and s.user_agent='pytest-agent'")
    assert row == [{"role": "user", "user_agent": "pytest-agent", "ended_at": None}]


def test_logout_ends_the_session_with_reason(client):
    tok = login_user(client)["token"]
    client.post("/user/logout", headers=bearer(tok))
    assert client.get("/user/me", headers=bearer(tok)).status_code == 401
    assert sql("select end_reason from sessions s join users u on u.id=s.user_id "
               "where u.email='s@test.local' and ended_at is not null") == [{"end_reason": "logout"}]


def test_idle_session_times_out(client):
    tok = login_user(client)["token"]
    sql("update sessions set last_seen_at = now() - interval '481 minutes' "
        "where user_id=(select id from users where email='s@test.local') returning 1")
    r = client.get("/user/me", headers=bearer(tok))
    assert r.status_code == 401 and "timed out" in r.json()["detail"]
    assert sql("select end_reason from sessions where user_id=(select id from users where email='s@test.local') "
               "and ended_at is not null") == [{"end_reason": "idle"}]


def test_admin_idle_timeout_is_shorter(client, make_admin):
    email, pw = make_admin()
    r = client.post("/admin/login", json={"email": email, "password": pw})
    assert r.json()["idle_timeout_sec"] == 30 * 60
    sql("update sessions set last_seen_at = now() - interval '31 minutes' "
        "where user_id=(select id from users where email=%s) returning 1", email)
    assert client.get("/admin/users", headers=bearer(r.json()["token"])).status_code == 401


def test_expired_session_is_recorded_as_expired(client):
    tok = login_user(client)["token"]
    sql("update sessions set expires_at = now() - interval '1 second' "
        "where user_id=(select id from users where email='s@test.local') returning 1")
    assert client.get("/user/me", headers=bearer(tok)).status_code == 401
    assert sql("select end_reason from sessions where user_id=(select id from users where email='s@test.local') "
               "and ended_at is not null") == [{"end_reason": "expired"}]


def test_activity_updates_last_seen_at_most_once_a_minute(client):
    tok = login_user(client)["token"]
    q = "select last_seen_at from sessions where user_id=(select id from users where email='s@test.local')"
    first = sql(q)[0]["last_seen_at"]
    client.get("/user/me", headers=bearer(tok))
    assert sql(q)[0]["last_seen_at"] == first  # within 60s: no write
    sql("update sessions set last_seen_at = now() - interval '2 minutes' "
        "where user_id=(select id from users where email='s@test.local') returning 1")
    stale = sql(q)[0]["last_seen_at"]
    client.get("/user/me", headers=bearer(tok))
    assert sql(q)[0]["last_seen_at"] > stale


def test_disabling_a_user_ends_their_sessions(client, make_admin):
    tok = login_user(client)["token"]
    email, pw = make_admin()
    atok = client.post("/admin/login", json={"email": email, "password": pw}).json()["token"]
    uid = sql("select id from users where email='s@test.local'")[0]["id"]
    client.patch(f"/admin/users/{uid}", json={"is_active": False}, headers=bearer(atok))
    assert client.get("/user/me", headers=bearer(tok)).status_code == 401
    assert sql("select end_reason from sessions where user_id=%s", uid) == [{"end_reason": "disabled"}]


def test_admin_sees_session_time_per_user(client, make_admin):
    tok = login_user(client)["token"]
    sql("update sessions set started_at = now() - interval '10 minutes', last_seen_at = now() - interval '2 minutes' "
        "where user_id=(select id from users where email='s@test.local') returning 1")
    email, pw = make_admin()
    atok = client.post("/admin/login", json={"email": email, "password": pw}).json()["token"]
    u = next(x for x in client.get("/admin/users", headers=bearer(atok)).json() if x["email"] == "s@test.local")
    assert u["active_now"] is True and u["session_count"] == 1
    assert 470 <= u["total_session_seconds"] <= 490 and u["last_session_seconds"] == u["total_session_seconds"]
    assert u["last_login_at"]

    client.post("/user/logout", headers=bearer(tok))
    u = next(x for x in client.get("/admin/users", headers=bearer(atok)).json() if x["email"] == "s@test.local")
    assert u["active_now"] is False

    rows = client.get(f"/admin/users/{u['id']}/sessions", headers=bearer(atok)).json()
    assert len(rows) == 1 and rows[0]["end_reason"] == "logout" and rows[0]["duration_sec"] >= 470
    assert "jti" not in rows[0]
    assert client.get(f"/admin/users/{u['id']}/sessions", headers=bearer(tok)).status_code == 401  # logged out
    assert client.get("/admin/users/999999/sessions", headers=bearer(atok)).status_code == 404
