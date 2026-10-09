import time

import jwt

from competitor_moves.core.security import JWT_ALG


def signup(c, email="a@test.local", password="password123"):
    return c.post("/user/signup", json={"email": email, "password": password})

def bearer(token):
    return {"Authorization": f"Bearer {token}"}

def test_signup_login_me_logout(client):
    r = signup(client)
    assert r.status_code == 201 and r.json()["role"] == "user"
    r = client.post("/user/login", json={"email": "A@test.local", "password": "password123"})  # case-insensitive
    assert r.status_code == 200
    tok = r.json()["token"]
    assert client.get("/user/me", headers=bearer(tok)).json()["email"] == "a@test.local"
    assert client.post("/user/logout", headers=bearer(tok)).status_code == 204
    assert client.get("/user/me", headers=bearer(tok)).status_code == 401  # revoked

def test_signup_rules(client):
    assert signup(client).status_code == 201
    assert signup(client).status_code == 409
    assert signup(client, email="b@test.local", password="short").status_code == 422
    assert signup(client, email="not-an-email").status_code == 422

def test_bad_login(client):
    signup(client)
    assert client.post("/user/login", json={"email": "a@test.local", "password": "wrong-pass"}).status_code == 401
    assert client.post("/user/login", json={"email": "nobody@test.local", "password": "password123"}).status_code == 401

def test_missing_and_forged_tokens(client):
    assert client.get("/user/me").status_code == 401
    assert client.get("/user/me", headers=bearer("garbage")).status_code == 401
    uid = signup(client).json()["id"]
    forged = jwt.encode({"sub": str(uid), "jti": "x", "exp": int(time.time()) + 60}, "wrong-key-wrong-key-wrong-key-12345", algorithm=JWT_ALG)
    assert client.get("/user/me", headers=bearer(forged)).status_code == 401
    expired = jwt.encode({"sub": str(uid), "jti": "y", "exp": int(time.time()) - 5}, "test-secret-test-secret-test-secret-123", algorithm=JWT_ALG)
    assert client.get("/user/me", headers=bearer(expired)).status_code == 401

def test_admin_login_is_separate(client, make_admin):
    email, pw = make_admin()
    signup(client)
    assert client.post("/admin/login", json={"email": "a@test.local", "password": "password123"}).status_code == 401  # user can't use admin login
    assert client.post("/user/login", json={"email": email, "password": pw}).status_code == 401  # admin can't use user login
    r = client.post("/admin/login", json={"email": email, "password": pw})
    assert r.status_code == 200 and r.json()["role"] == "admin"

def test_admin_manages_users(client, make_admin):
    email, pw = make_admin()
    uid = signup(client).json()["id"]
    utok = client.post("/user/login", json={"email": "a@test.local", "password": "password123"}).json()["token"]
    atok = client.post("/admin/login", json={"email": email, "password": pw}).json()["token"]

    assert client.get("/admin/users", headers=bearer(utok)).status_code == 403  # plain user blocked
    assert client.get("/admin/users").status_code == 401
    emails = [u["email"] for u in client.get("/admin/users", headers=bearer(atok)).json()]
    assert "a@test.local" in emails and "boss@test.local" in emails
    assert "password_hash" not in client.get("/admin/users", headers=bearer(atok)).text

    assert client.patch(f"/admin/users/{uid}", json={"is_active": False}, headers=bearer(atok)).status_code == 200
    assert client.get("/user/me", headers=bearer(utok)).status_code == 401  # deactivated user's token stops working
    assert client.post("/user/login", json={"email": "a@test.local", "password": "password123"}).status_code == 403  # disabled

    admin_id = next(u["id"] for u in client.get("/admin/users", headers=bearer(atok)).json() if u["role"] == "admin" and u["email"] == "boss@test.local")
    assert client.patch(f"/admin/users/{admin_id}", json={"is_active": False}, headers=bearer(atok)).status_code == 404  # admins untouchable via API
    assert client.delete(f"/admin/users/{uid}", headers=bearer(atok)).status_code == 204

def test_admin_logout(client, make_admin):
    email, pw = make_admin()
    atok = client.post("/admin/login", json={"email": email, "password": pw}).json()["token"]
    assert client.post("/admin/logout", headers=bearer(atok)).status_code == 204
    assert client.get("/admin/users", headers=bearer(atok)).status_code == 401
