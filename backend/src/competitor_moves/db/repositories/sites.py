def for_crawl(conn, site_id: int) -> dict | None:
    """The site plus what a crawl needs from its project and owner."""
    return conn.execute("""
        select s.*, p.industry, p.owner_user_id, p.name as project_name
        from sites s join projects p on p.id = s.project_id where s.id = %s""", (site_id,)).fetchone()


def our_site_id(conn, project_id: int) -> int | None:
    row = conn.execute("select id from sites where project_id=%s and role='ours' and status <> 'removed'",
                       (project_id,)).fetchone()
    return row["id"] if row else None


def after_run(conn, site_id: int, *, platform: str | None, blocked: bool, error: str | None) -> None:
    conn.execute("""
        update sites set last_run_at = now(), platform = coalesce(%s, platform), last_error = %s,
               status = case when %s then 'blocked' else status end
        where id = %s""", (platform, error, blocked, site_id))
