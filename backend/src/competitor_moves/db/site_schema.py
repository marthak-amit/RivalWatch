"""One Postgres schema per website (`site_<id>`), all created from the same template.

Schema names are built only from an integer id, never from user text, and are always quoted.
"""
from importlib.resources import files

from psycopg import Connection, sql

_TEMPLATE = files("competitor_moves.db").joinpath("site_schema.sql").read_text()


def schema_name(site_id: int) -> str:
    if isinstance(site_id, bool) or not isinstance(site_id, int) or site_id < 1:
        raise ValueError(f"invalid site id: {site_id!r}")
    return f"site_{site_id}"


def create_site_schema(conn: Connection, site_id: int) -> str:
    """Idempotent: safe to run again on an existing site."""
    name = schema_name(site_id)
    conn.execute(sql.SQL(_TEMPLATE).format(schema=sql.Identifier(name)))
    return name


def drop_site_schema(conn: Connection, site_id: int) -> None:
    conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema_name(site_id))))


def table(site_id: int, name: str) -> sql.Composed:
    """Quoted `schema.table` for use inside queries. `name` must be one of the template's tables."""
    if name not in {"products", "price_history", "pages", "crawl_runs", "events"}:
        raise ValueError(f"unknown site table: {name!r}")
    return sql.SQL("{}.{}").format(sql.Identifier(schema_name(site_id)), sql.Identifier(name))
