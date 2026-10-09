import psycopg
import pytest
from psycopg import sql

from competitor_moves.db.site_schema import create_site_schema, schema_name, table


def insert(conn, site_id, name, cols, values):
    q = sql.SQL("insert into {} ({}) values ({}) returning *").format(
        table(site_id, name), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)))
    return conn.execute(q, values).fetchone()


def count(conn, site_id, name):
    return conn.execute(sql.SQL("select count(*) as n from {}").format(table(site_id, name))).fetchone()["n"]


def test_each_website_has_its_own_tables_and_data(conn, make_site):
    a, b = make_site("https://a.example/"), make_site("https://b.example/")
    insert(conn, a, "products", ["url", "sku"], ["https://a.example/p1", "X1"])
    assert (count(conn, a, "products"), count(conn, b, "products")) == (1, 0)
    for t in ("products", "price_history", "pages", "crawl_runs", "history"):
        count(conn, a, t)
    assert create_site_schema(conn, a) == f"site_{a}"  # idempotent: running it again keeps the data
    assert count(conn, a, "products") == 1


def test_history_rows_are_tied_to_their_site_run_product_and_user(conn, make_site):
    sid = make_site("https://h.example/")
    # a person who changed something on this site (e.g. an admin); not the workspace owner, whose deletion
    # deletes the whole workspace by design
    uid = conn.execute("insert into users(email,password_hash,role) values ('editor@test.local','x','admin') returning id"
                       ).fetchone()["id"]
    run = insert(conn, sid, "crawl_runs", ["run_type"], ["first"])
    prod = insert(conn, sid, "products", ["url"], ["https://h.example/p"])
    h = insert(conn, sid, "history", ["run_id", "product_id", "user_id", "type", "occurred_at"],
               [run["id"], prod["id"], uid, "price_drop", "2026-10-09"])
    assert h["site_id"] == sid  # filled in automatically
    with pytest.raises(psycopg.errors.CheckViolation):  # a row can't claim to belong to another website
        insert(conn, sid, "history", ["site_id", "type", "occurred_at"], [sid + 1, "x", "2026-10-09"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # nor point at a user that doesn't exist
        insert(conn, sid, "history", ["user_id", "type", "occurred_at"], [10**9, "x", "2026-10-09"])
    conn.execute("delete from users where id=%s", (uid,))  # deleting that person keeps the history, without the link
    row = conn.execute(sql.SQL("select user_id, product_id from {} where id=%s").format(table(sid, "history")), (h["id"],)).fetchone()
    assert row == {"user_id": None, "product_id": prod["id"]}


def test_deleting_a_site_row_removes_its_data(conn, make_site):
    sid = make_site("https://gone.example/")
    insert(conn, sid, "products", ["url"], ["https://gone.example/p"])
    conn.execute("delete from sites where id=%s", (sid,))
    assert count(conn, sid, "products") == 0


@pytest.mark.parametrize("bad", [0, -1, "1; drop schema public", "site_1", 1.5, True, None])
def test_schema_name_rejects_anything_but_a_positive_int(bad):
    with pytest.raises(ValueError):
        schema_name(bad)


def test_table_rejects_unknown_table_names():
    with pytest.raises(ValueError):
        table(1, "users; drop table users")


def test_site_limits_default_to_null_and_reject_non_positive(conn, make_site):
    sid = make_site("https://limits.example/")
    row = conn.execute("select max_products, page_budget from sites where id=%s", (sid,)).fetchone()
    assert row == {"max_products": None, "page_budget": None}
    conn.execute("update sites set max_products=150 where id=%s", (sid,))
    for col in ("max_products", "page_budget"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(f"update sites set {col}=0 where id=%s", (sid,))


def test_deleting_the_owner_deletes_their_workspace(conn, make_site):
    sid = make_site("https://owned.example/", owner_email="owner2@test.local")
    insert(conn, sid, "products", ["url"], ["https://owned.example/p"])
    conn.execute("delete from users where email='owner2@test.local'")
    assert conn.execute("select count(*) as n from sites where id=%s", (sid,)).fetchone()["n"] == 0
    assert count(conn, sid, "products") == 0
