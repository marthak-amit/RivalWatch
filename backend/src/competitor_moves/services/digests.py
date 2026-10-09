"""Digests: the analyst's (or the rules') summary and 3-5 actions for a workspace, stored in `digests`.

The facts come from code (changes, comparison, shared SKUs), each line with an evidence id. The AI only words them,
and its actions are checked: unknown evidence ids are dropped, an action with none left is dropped, and every number in
an action must appear in the lines it cites. Fewer than 3 left: topped up from the rule-based digest.
"""
import hashlib
import json
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from . import comparison, dashboard, workspaces
from .moves import _numbers

MAX_CHANGES, MAX_POSITIONS, MAX_SKUS = 60, 15, 15
CRAWL_JOBS = ("crawl_site", "sync_store")


def _ours(conn, project_id: int) -> dict | None:
    return conn.execute("select * from sites where project_id=%s and role='ours'", (project_id,)).fetchone()


def last(conn, project_id: int) -> dict | None:
    return conn.execute("select * from digests where project_id=%s order by id desc limit 1", (project_id,)).fetchone()


def gather(conn, project: dict, since: datetime) -> dict:
    """Everything the analyst may use, as id -> line."""
    ours = _ours(conn, project["id"])
    comps = workspaces.competitors(conn, project["id"])
    changes = dashboard.changes(conn, comps, since=since)[:MAX_CHANGES]
    lines: dict[str, dict[str, str]] = {"changes": {}, "positioning": {}, "shared_skus": {}}
    for c in changes:
        lines["changes"][f"e:{c['id']}"] = f"{c['competitor']} | {c['label']}"
    cmp = comparison.compare(conn, project, ours, comps) if ours else None
    if cmp:
        n = 0
        for row in cmp["rows"]:
            o = row["ours"]
            for t in row["competitors"]:
                if "gapPct" not in t or n >= MAX_POSITIONS:
                    continue
                n += 1
                lines["positioning"][f"p:{n}"] = (
                    f"{row['label']} | ours: {o['count']}, median {o['median']} {o['currency']}, {o['min']}-{o['max']} | "
                    f"{t['name']}: {t['count']}, median {t['medianInOurCurrency']} {o['currency']}, gap {t['gapPct']}%")
        for row in cmp["onlyCompetitors"][:5]:
            for t in row["competitors"]:
                n += 1
                lines["positioning"][f"p:{n}"] = (f"{row['label']} | ours: 0 products | {t['name']}: {t['count']}, "
                                                  f"{t['min']}-{t['max']} {t['currency']}")
        for m in cmp["matches"][:MAX_SKUS]:
            if m["gapPct"] is not None:
                lines["shared_skus"][f"s:{m['sku']}"] = (f"{m['title']} | our price {m['ours']['price']} {m['ours']['currency']} | "
                                                         f"{m['competitor']} {m['theirs']['price']} {m['theirs']['currency']} | gap {m['gapPct']}%")
    return {"lines": lines, "changes": changes, "our_store": (ours or {}).get("name") or "our store",
            "industry": project.get("industry") or "jewellery", "since": since}


def inputs_hash(lines: dict) -> str:
    return hashlib.sha1(json.dumps(lines, sort_keys=True).encode()).hexdigest()


def evidence_map(lines: dict) -> dict[str, str]:
    return {k: v for group in lines.values() for k, v in group.items()}


def prompt_blocks(lines: dict) -> dict[str, str]:
    return {name: "\n".join(f"{k} | {v}" for k, v in group.items()) or "(none)" for name, group in lines.items()}


def validate(draft: dict, evidence: dict[str, str]) -> list[dict]:
    """Keep only actions backed by real evidence whose numbers all come from the lines they cite."""
    out = []
    for a in draft.get("actions") or []:
        cited = [e for e in a.get("evidence") or [] if e in evidence]
        if not cited:
            continue
        allowed = [n for e in cited for n in _numbers(evidence[e])]
        text = f"{a.get('title', '')} {a.get('why', '')}"
        if not all(any(n == x or round(x) == n for x in allowed) for n in _numbers(text)):
            continue
        out.append({"title": a["title"][:80], "why": a["why"][:240], "priority": a["priority"], "evidence": cited})
    return out[:5]


def finish(draft: dict | None, gathered: dict) -> dict:
    """The digest to store: validated AI actions, topped up (or replaced) by the rule-based digest."""
    evidence = evidence_map(gathered["lines"])
    rules = dashboard.rules_digest(gathered["changes"]) if gathered["changes"] else None
    actions = validate(draft, evidence) if draft else []
    if draft and actions:
        for r in (rules or {}).get("actions", []):
            if len(actions) >= 3:
                break
            actions.append({**r, "evidence": []})
        known = [x for line in evidence.values() for x in _numbers(line)]
        summary_ok = all(any(n == x or round(x) == n for x in known) for n in _numbers(draft.get("summary") or ""))
        summary = draft["summary"] if summary_ok else (rules or {}).get("summary") or draft["summary"]
        return {"summary": summary[:600], "actions": actions,
                "source": "gemini" if len([a for a in actions if a["evidence"]]) >= 3 else "gemini+rules"}
    if rules:
        return {**rules, "actions": [{**a, "evidence": []} for a in rules["actions"]], "source": "rules"}
    return {"summary": "No competitor changes since the last digest.", "actions": [], "source": "rules"}


def save(conn, project_id: int, digest: dict, gathered: dict, *, model: str | None, prompt_version: str | None) -> dict:
    return conn.execute(
        "insert into digests(project_id, change_count, summary, actions, evidence, source, inputs_hash, prompt_version, model) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (project_id, len(gathered["changes"]), digest["summary"], Jsonb(digest["actions"]),
         Jsonb(evidence_map(gathered["lines"])), digest["source"], inputs_hash(gathered["lines"]), prompt_version, model)
    ).fetchone()


def ui_list(conn, project_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute("select * from digests where project_id=%s order by id desc limit %s", (project_id, limit)).fetchall()
    return [{"ts": r["created_at"].astimezone(UTC).isoformat().replace("+00:00", "Z"), "changeCount": r["change_count"],
             "summary": r["summary"], "actions": r["actions"], "source": r["source"], "evidence": r["evidence"]} for r in rows]


def since_last(conn, project_id: int) -> datetime:
    d = last(conn, project_id)
    return d["created_at"] if d else datetime.now(UTC) - timedelta(days=7)


def maybe_queue(conn, site_id: int) -> bool:
    """After a crawl or store sync: once none of the project's crawls/syncs are still waiting, queue its digest."""
    site = conn.execute("select project_id from sites where id=%s", (site_id,)).fetchone()
    if not site:
        return False
    busy = conn.execute(
        "select 1 from jobs j join sites s on s.id = (j.payload->>'site_id')::int where s.project_id=%s "
        "and j.type = any(%s) and j.status in ('queued','running') and j.payload->>'site_id' <> %s limit 1",
        (site["project_id"], list(CRAWL_JOBS), str(site_id))).fetchone()
    if busy:
        return False
    return conn.execute("insert into jobs(type, payload) values ('compare_project', %s) on conflict do nothing returning id",
                        (Jsonb({"project_id": site["project_id"]}),)).fetchone() is not None
