from datetime import date

from psycopg.types.json import Jsonb


def previous_copy(conn, site_id: int) -> dict[str, dict]:
    """Last AI wording per move key, reused when a run finds nothing new (no LLM call)."""
    rows = conn.execute("select type, category, headline, why_it_matters from moves where site_id=%s and not ai_observed",
                        (site_id,)).fetchall()
    return {f"{r['type']}|{r['category'] or ''}": r for r in rows}


def replace_for_site(conn, project_id: int, site_id: int, window_from: date, window_to: date,
                     moves: list[dict], notable: list[dict]) -> None:
    """Rewrite the site's moves for the window. Notable (AI-observed) moves are kept until they leave the window."""
    with conn.transaction():
        conn.execute("delete from moves where site_id=%s and (not ai_observed or window_to < %s)", (site_id, window_from))
        for m in moves + notable:
            conn.execute("""
                insert into moves(project_id, site_id, type, category, headline, why_it_matters, size, evidence,
                                  ai_observed, window_from, window_to)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (project_id, site_id, m["type"], m.get("category"), m["headline"], m.get("why_it_matters") or None,
                 Jsonb(m.get("size") or {}), Jsonb(m.get("evidence") or []), m.get("ai_observed", False),
                 window_from, window_to))
