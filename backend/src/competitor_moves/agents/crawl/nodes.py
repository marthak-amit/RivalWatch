"""Crawl graph nodes (agent spec §1.3). Only `researcher` and `summarize` call the LLM; everything else is code."""
import json
import logging
import time
from collections import Counter
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from ...db.repositories import credits, site_data
from ...db.repositories import moves as moves_repo
from ...db.repositories import sites as sites_repo
from ...services import change_detection as cd
from ...services import moves
from ...services import site_crawl as sc
from .prompts import PROMPT_VERSION, RESEARCHER_SYSTEM, SUMMARIZE, RunCopy
from .tools import TOOLS_BY_NAME

log = logging.getLogger("crawl")
MAX_FACT_MOVES = 30
HEADLINE_MAX, WHY_MAX, NOTABLE_MAX = 120, 200, 3


class CrawlState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    steps: int
    llm_failed: bool
    skip: bool
    has_changes: bool
    facts: dict | None
    copy: dict | None


def run_of(config) -> sc.RunContext:
    return config["configurable"]["run"]


class CrawlNodes:
    def __init__(self, researcher_llm, summarize_llm, *, max_steps: int, retry_delay: float = 1.0):
        self.researcher_llm = researcher_llm  # already bound to the tools
        self.summarize_llm = summarize_llm
        self.max_steps = max_steps
        self.retry_delay = retry_delay

    # ---- load_context -----------------------------------------------------------------------------------
    def load_context(self, state: CrawlState, config) -> dict:
        ctx = run_of(config)
        ctx.run_id = site_data.start_run(ctx.conn, ctx.site_id, ctx.run_type)
        if ctx.site["status"] != "active":
            ctx.stop_reason = "SKIPPED"
            return {"skip": True}
        if ctx.charges_credits and credits.balance(ctx.conn, ctx.site["owner_user_id"]) <= 0:
            ctx.stop_reason = "NO CREDITS"
            return {"skip": True}
        known = [n["name"] for n in (ctx.prev_home or {}).get("nav") or []]
        system = RESEARCHER_SYSTEM.format(
            industry=ctx.site["industry"], domain=ctx.site["domain"], platform=ctx.site["platform"] or "unknown",
            run_type=ctx.run_type, last_run_at=ctx.prev_run["finished_at"].strftime("%Y-%m-%d %H:%M UTC") if ctx.prev_run else "never",
            days_since_last_run=ctx.days_since_last_run, known_products=ctx.known_products, max_products=ctx.max_products,
            page_budget=ctx.page_budget,
            credits_left=credits.balance(ctx.conn, ctx.site["owner_user_id"]) if ctx.charges_credits else "unlimited",
            known_categories=", ".join(f'"{n}"' for n in known[:20]) or "none yet",
            scope=ctx.scope_text, sort_label=sc.SORT_LABELS[ctx.sort])
        return {"messages": [SystemMessage(system), HumanMessage(f"Start the {ctx.run_type} run for {ctx.site['domain']}.")],
                "steps": 0}

    def route_after_load(self, state: CrawlState, config) -> str:
        if state.get("skip"):
            return "finalize"
        return "researcher" if self.researcher_llm is not None else "fallback_plan"

    # ---- researcher + tools -----------------------------------------------------------------------------
    def researcher(self, state: CrawlState, config) -> dict:
        ctx, last_error = run_of(config), None
        for attempt in range(2):
            try:
                reply = self.researcher_llm.invoke(state["messages"])
                return {"messages": [reply], "steps": state.get("steps", 0) + 1}
            except Exception as e:  # noqa: BLE001 - any model/network failure means: retry, then fall back
                last_error = e
                time.sleep(self.retry_delay * (attempt + 1))
        ctx.trace.append({"node": "researcher", "error": f"{type(last_error).__name__}: {last_error}"[:300]})
        log.warning("researcher failed for site %s, using the fallback plan: %s", ctx.site_id, last_error)
        return {"llm_failed": True}

    def route_after_researcher(self, state: CrawlState, config) -> str:
        if state.get("llm_failed"):
            return "fallback_plan"
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("steps", 0) <= self.max_steps and not run_of(config).stop_reason:
            return "tools"
        if not getattr(last, "tool_calls", None):
            run_of(config).trace.append({"node": "researcher", "final": str(last.text)[:300]})
        return "collect_changes"

    def tools(self, state: CrawlState, config) -> dict:
        """Run the requested tool calls one at a time (they share the run's counters and DB connection)."""
        out = []
        for call in state["messages"][-1].tool_calls:
            tool = TOOLS_BY_NAME.get(call["name"])
            if tool is None:
                content = f"ERROR: unknown tool {call['name']}"
            else:
                try:
                    content = tool.invoke(call["args"], config)
                except (ValidationError, TypeError, ValueError) as e:
                    content = f"ERROR: invalid arguments for {call['name']}: {str(e).splitlines()[0][:200]}"
            out.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        return {"messages": out}

    def fallback_plan(self, state: CrawlState, config) -> dict:
        ctx = run_of(config)
        ctx.trace.append({"node": "fallback_plan"})
        sc.run_default_plan(ctx)
        return {}

    # ---- changes and wording ----------------------------------------------------------------------------
    def collect_changes(self, state: CrawlState, config) -> dict:
        ctx = run_of(config)
        if ctx.full_coverage:  # the whole catalog was read: products missing twice in a row are removed
            ctx.events += cd.mark_missing(ctx.conn, ctx.site_id, ctx.run_id, ctx.products_seen, now=ctx.started)
        groups = moves.group(site_data.window_events(ctx.conn, ctx.site_id, ctx.window_start))
        ctx.groups = groups
        our_id = sites_repo.our_site_id(ctx.conn, ctx.site["project_id"])
        facts = {"moves": [_fact(g) for g in groups[:MAX_FACT_MOVES]],
                 "our_categories": [] if our_id in (None, ctx.site_id) else site_data.categories(ctx.conn, our_id)}
        page = next((e for e in ctx.events if e["type"] == "page_changed"), None)
        if page:
            facts["homepage"] = {"before": page["before"], "after": page["after"]}
        return {"has_changes": bool(ctx.events), "facts": facts}

    def route_after_collect(self, state: CrawlState, config) -> str:
        return "summarize" if state.get("has_changes") and self.summarize_llm is not None else "finalize"

    def summarize(self, state: CrawlState, config) -> dict:
        ctx = run_of(config)
        prompt = SUMMARIZE.format(
            industry=ctx.site["industry"], domain=ctx.site["domain"],
            site_role="our store" if ctx.site["role"] == "ours" else "competitor",
            window_from=ctx.window_start.date().isoformat(), window_to=ctx.started.date().isoformat(),
            facts_json=json.dumps(state["facts"], default=str, ensure_ascii=False, indent=1))
        for attempt in range(2):
            try:
                out = self.summarize_llm.with_structured_output(RunCopy).invoke(prompt)
                if out is not None:
                    return {"copy": RunCopy.model_validate(out).model_dump()}
            except Exception as e:  # noqa: BLE001 - wording is optional: templates are used instead
                ctx.trace.append({"node": "summarize", "error": f"{type(e).__name__}: {e}"[:300]})
                time.sleep(self.retry_delay * (attempt + 1))
        return {"copy": None}

    # ---- finalize ---------------------------------------------------------------------------------------
    def finalize(self, state: CrawlState, config) -> dict:
        ctx = run_of(config)
        groups = ctx.groups
        if groups is None:  # skipped runs never reached collect_changes
            groups = moves.group(site_data.window_events(ctx.conn, ctx.site_id, ctx.window_start))
        copy = state.get("copy") or {}
        by_key = {m["key"]: m for m in copy.get("moves") or []}
        prev = moves_repo.previous_copy(ctx.conn, ctx.site_id)
        rows = [_move_row(g, by_key.get(g["key"]), prev.get(g["key"]), ours=ctx.site["role"] == "ours") for g in groups]
        notable = [{"type": "notable", "category": None, "headline": n["headline"][:HEADLINE_MAX], "size": {},
                    "evidence": [{"before": n["before"][:200], "after": n["after"][:200]}], "ai_observed": True}
                   for n in (copy.get("notable") or [])[:NOTABLE_MAX] if n.get("headline")]
        moves_repo.replace_for_site(ctx.conn, ctx.site["project_id"], ctx.site_id, ctx.window_start.date(),
                                    ctx.started.date(), rows, notable)
        status = _status(ctx)
        summary = _run_summary(ctx, copy, rows, status)
        site_data.finish_run(
            ctx.conn, ctx.site_id, ctx.run_id, status=status, pages_fetched=ctx.pages_used, pages_failed=ctx.pages_failed,
            products_seen=len(ctx.products_seen), est_total_products=ctx.est_total, limit_hit=ctx.limit_hit,
            full_coverage=ctx.full_coverage, stop_reason=ctx.stop_label,
            changes=dict(Counter(e["type"] for e in ctx.events)), summary=summary, trace=ctx.trace,
            home=ctx.home or ctx.prev_home, prompt_version=PROMPT_VERSION, models=ctx.models)
        sites_repo.after_run(ctx.conn, ctx.site_id, platform=ctx.platform, blocked=ctx.stop_reason == "BLOCKED",
                             error=summary if status == "failed" else None)
        ctx.outcome, ctx.summary = status, summary
        ctx.close()
        return {}


def _fact(g: dict) -> dict:
    """What the summarize prompt sees of a move: no ids, short examples."""
    return {"key": g["key"], "type": g["type"], "category": g["category"], "size": g["size"],
            "promotions": g["promotions"][:3],
            "examples": [{"title": (e["title"] or "")[:120], "before": e["before"], "after": e["after"]}
                         for e in g["examples"]]}


def _move_row(g: dict, ai: dict | None, prev: dict | None, *, ours: bool) -> dict:
    """AI text if it passes the number check, else last run's AI text if still accurate, else the template."""
    def pick(field: str, limit: int) -> str:
        for source in (ai, prev):
            text = ((source or {}).get(field) or "").strip()
            if text and len(text) <= limit and moves.numbers_ok(text, g):
                return text
        return ""
    headline = pick("headline", HEADLINE_MAX) or moves.template_headline(g)
    why = "" if ours else pick("why_it_matters", WHY_MAX)
    evidence = [{**e, "occurred_at": e["occurred_at"].isoformat()} for e in g["examples"]]
    return {"type": g["type"], "category": g["category"], "headline": headline, "why_it_matters": why,
            "size": g["size"], "evidence": evidence or [{"event_ids": g["evidence_event_ids"][:20]}]}


def _status(ctx: sc.RunContext) -> str:
    if ctx.stop_reason == "SKIPPED" or (ctx.stop_reason == "NO CREDITS" and ctx.pages_used == 0):
        return "skipped"
    if ctx.stop_reason == "BLOCKED":
        return "blocked"
    if ctx.pages_used == 0 or (ctx.pages_used == ctx.pages_failed and not ctx.products_seen):
        return "failed"
    if ctx.stop_reason in ("NO CREDITS", "TIME LIMIT"):
        return "partial"
    return "ok"


def _run_summary(ctx: sc.RunContext, copy: dict, rows: list[dict], status: str) -> str:
    domain = ctx.site["domain"]
    if status == "skipped":
        why = "no crawl credits left" if ctx.stop_reason == "NO CREDITS" else f"site is {ctx.site['status']}"
        return f"Skipped {domain}: {why}."
    if status == "blocked":
        return f"{domain} blocks our crawler (robots.txt or repeated 403/429); nothing was collected."
    if status == "failed":
        first_error = next((t["result"] for t in ctx.trace if str(t.get("result", "")).startswith("ERROR")), "no pages fetched")
        return f"Could not crawl {domain}: {first_error}"[:300]
    if not ctx.events:
        since = ctx.prev_run["finished_at"].strftime("%Y-%m-%d") if ctx.prev_run else "the first look"
        return (f"No changes on {domain} since {since}; checked {ctx.pages_used} pages, "
                f"{len(ctx.products_seen)} products.")
    text = (copy.get("run_summary") or "").strip()
    pool = {"size": {k: v for r in rows for k, v in r["size"].items() if isinstance(v, (int, float))}, "category": " ".join(
        r["category"] or "" for r in rows), "examples": [{"title": r["headline"]} for r in rows]}
    if text and len(text) <= 400 and moves.numbers_ok(text, pool):
        return text
    return "; ".join(r["headline"] for r in rows[:3]) or f"{len(ctx.events)} changes on {domain}."
