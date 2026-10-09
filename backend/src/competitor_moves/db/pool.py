from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..config import get_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily create the pool. autocommit: each statement commits; use `conn.transaction()` for groups."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=False,
                               kwargs={"row_factory": dict_row, "autocommit": True})
        _pool.open()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_conn():
    with get_pool().connection() as conn:
        yield conn
