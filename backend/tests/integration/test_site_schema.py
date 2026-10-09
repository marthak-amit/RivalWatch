import psycopg
import pytest

from competitor_moves.db.site_schema import create_site_schema, drop_site_schema, schema_name, table

A, B = 9001, 9002  # ids far from real sites


@pytest.fixture
def two_sites(conn):
    create_site_schema(conn, A)
    create_site_schema(conn, B)
    yield
    drop_site_schema(conn, A)
    drop_site_schema(conn, B)


def test_data_stays_in_its_own_site(conn, two_sites):
    conn.execute(psycopg.sql.SQL("insert into {} (url, sku) values ('https://a/p1','X1')").format(table(A, "products")))
    n_a = conn.execute(psycopg.sql.SQL("select count(*) as n from {}").format(table(A, "products"))).fetchone()["n"]
    n_b = conn.execute(psycopg.sql.SQL("select count(*) as n from {}").format(table(B, "products"))).fetchone()["n"]
    assert (n_a, n_b) == (1, 0)


def test_create_is_idempotent(conn, two_sites):
    assert create_site_schema(conn, A) == "site_9001"  # second run must not fail or wipe anything


def test_all_template_tables_exist(conn, two_sites):
    for t in ("products", "price_history", "pages", "crawl_runs", "events"):
        conn.execute(psycopg.sql.SQL("select 1 from {} limit 1").format(table(A, t)))


@pytest.mark.parametrize("bad", [0, -1, "1; drop schema public", "site_1", 1.5, True, None])
def test_schema_name_rejects_anything_but_a_positive_int(bad):
    with pytest.raises(ValueError):
        schema_name(bad)


def test_table_rejects_unknown_table_names():
    with pytest.raises(ValueError):
        table(1, "users; drop table users")


def test_site_limits_default_to_null_and_reject_non_positive(conn):
    import psycopg
    conn.execute("delete from projects where name='limits-test'")
    pid = conn.execute("insert into projects(name,url) values ('limits-test','https://x.io') returning id").fetchone()["id"]
    try:
        row = conn.execute("insert into sites(project_id,domain,role,schema_name) values (%s,'a.com','competitor','site_9201') returning max_products,page_budget",
                           (pid,)).fetchone()
        assert row == {"max_products": None, "page_budget": None}
        conn.execute("update sites set max_products=150 where project_id=%s", (pid,))
        for col in ("max_products", "page_budget"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(f"update sites set {col}=0 where project_id=%s", (pid,))
    finally:
        conn.execute("delete from projects where id=%s", (pid,))
