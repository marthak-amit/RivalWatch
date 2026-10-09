class InsufficientCredits(Exception):
    pass


def balance(conn, user_id: int) -> int:
    return conn.execute("select credits from users where id=%s", (user_id,)).fetchone()["credits"]


def charge(conn, user_id: int, n: int = 1) -> int:
    """Atomically take n credits; returns what is left. Raises InsufficientCredits without taking any."""
    row = conn.execute("update users set credits = credits - %s where id=%s and credits >= %s returning credits",
                       (n, user_id, n)).fetchone()
    if row is None:
        raise InsufficientCredits("insufficient credits")
    return row["credits"]
