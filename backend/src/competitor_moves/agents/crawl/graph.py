"""The crawl graph (agent spec §1.1): load_context → researcher ⇄ tools → collect_changes → summarize → finalize."""
import logging

from langgraph.graph import END, START, StateGraph

from ...config import get_settings
from ...db.repositories import site_data
from ...services.site_crawl import RunContext
from ..llm import chat_model, model_name
from .extract_ai import make_extractor
from .nodes import CrawlNodes, CrawlState
from .tools import TOOLS

log = logging.getLogger("crawl")
DEFAULT = object()


def build(researcher_llm=None, summarize_llm=None, *, max_steps: int | None = None, retry_delay: float = 1.0):
    max_steps = max_steps or get_settings().max_agent_steps
    bound = researcher_llm.bind_tools(TOOLS) if researcher_llm is not None else None
    n = CrawlNodes(bound, summarize_llm, max_steps=max_steps, retry_delay=retry_delay)
    g = StateGraph(CrawlState)
    for name in ("load_context", "researcher", "tools", "fallback_plan", "collect_changes", "summarize", "finalize"):
        g.add_node(name, getattr(n, name))
    g.add_edge(START, "load_context")
    g.add_conditional_edges("load_context", n.route_after_load, ["researcher", "fallback_plan", "finalize"])
    g.add_conditional_edges("researcher", n.route_after_researcher, ["tools", "collect_changes", "fallback_plan"])
    g.add_edge("tools", "researcher")
    g.add_edge("fallback_plan", "collect_changes")
    g.add_conditional_edges("collect_changes", n.route_after_collect, ["summarize", "finalize"])
    g.add_edge("summarize", "finalize")
    g.add_edge("finalize", END)
    return g.compile(), max_steps


def run_site_crawl(conn, site_id: int, *, researcher_llm=DEFAULT, summarize_llm=DEFAULT, extract_llm=DEFAULT,
                   retry_delay: float = 1.0, max_steps: int | None = None) -> dict:
    """Crawl one site once. Returns the run's id, status and summary."""
    researcher_llm = chat_model("researcher") if researcher_llm is DEFAULT else researcher_llm
    summarize_llm = chat_model("summarize") if summarize_llm is DEFAULT else summarize_llm
    graph, max_steps = build(researcher_llm, summarize_llm, max_steps=max_steps, retry_delay=retry_delay)
    extract_llm = chat_model("extract") if extract_llm is DEFAULT else extract_llm
    ctx = RunContext.load(conn, site_id)
    ctx.models = {"researcher": model_name(researcher_llm), "summarize": model_name(summarize_llm),
                  "extract": model_name(extract_llm)}
    if extract_llm is not None:
        ctx.ai_extract = make_extractor(extract_llm, ctx.site["industry"])
    try:
        graph.invoke({"messages": [], "steps": 0}, config={"configurable": {"run": ctx}, "recursion_limit": 2 * max_steps + 12})
    except Exception as e:
        log.exception("crawl of site %s failed", site_id)
        if ctx.run_id:  # close the run row so the run log shows the failure
            site_data.finish_run(conn, site_id, ctx.run_id, status="failed", error=f"{type(e).__name__}: {e}"[:500],
                                 pages_fetched=ctx.pages_used, trace=ctx.trace)
        raise
    finally:
        ctx.close()  # the headless browser, if this run started one
    return {"site_id": site_id, "run_id": ctx.run_id, "status": ctx.outcome, "summary": ctx.summary,
            "pages": ctx.pages_used, "products": len(ctx.products_seen), "events": len(ctx.events)}


def run_job(conn, job: dict) -> dict:
    """Worker handler for 'crawl_site' jobs."""
    return run_site_crawl(conn, int(job["payload"]["site_id"]))
