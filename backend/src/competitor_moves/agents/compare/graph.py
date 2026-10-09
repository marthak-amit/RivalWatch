"""Compare graph (agent spec §2): gather → (nothing new: stop) → analyst → validate → save. Only `analyst` calls the LLM;
when it fails or isn't configured, the rule-based digest is saved instead."""
import logging
import time
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ...services import digests
from ..llm import chat_model, model_name
from .prompts import ANALYST, PROMPT_VERSION, Digest

log = logging.getLogger("compare")
DEFAULT = object()


class CompareState(TypedDict, total=False):
    gathered: dict
    has_new: bool
    draft: dict | None
    saved: dict | None


def build(llm, *, retry_delay: float = 1.0):
    def gather(state, config):
        c = config["configurable"]
        g = digests.gather(c["conn"], c["project"], c["since"])
        prev = digests.last(c["conn"], c["project"]["id"])
        unchanged = prev is not None and prev["inputs_hash"] == digests.inputs_hash(g["lines"])
        has_facts = any(g["lines"][k] for k in g["lines"])
        return {"gathered": g, "has_new": has_facts and not unchanged and (bool(g["changes"]) or prev is None or c["force"])}

    def route(state, config):
        if not state["has_new"]:
            return END
        return "analyst" if llm is not None else "save"

    def analyst(state, config):
        g = state["gathered"]
        blocks = digests.prompt_blocks(g["lines"])
        prompt = ANALYST.format(our_store=g["our_store"], industry=g["industry"], since=g["since"].strftime("%Y-%m-%d"),
                                changes=blocks["changes"], positioning=blocks["positioning"], shared_skus=blocks["shared_skus"])
        for attempt in range(2):
            try:
                out = llm.with_structured_output(Digest).invoke(prompt)
                if out is not None:
                    return {"draft": Digest.model_validate(out).model_dump()}
            except Exception as e:  # noqa: BLE001 - any model failure: the rule-based digest is used instead
                log.warning("analyst failed: %s", e)
                time.sleep(retry_delay * (attempt + 1))
        return {"draft": None}

    def save(state, config):
        c = config["configurable"]
        digest = digests.finish(state.get("draft"), state["gathered"])
        if not c["store"]:
            return {"saved": digest}
        row = digests.save(c["conn"], c["project"]["id"], digest, state["gathered"], model=model_name(llm),
                           prompt_version=PROMPT_VERSION if llm is not None else None)
        return {"saved": {**digest, "id": row["id"]}}

    g = StateGraph(CompareState)
    g.add_node("gather", gather)
    g.add_node("analyst", analyst)
    g.add_node("save", save)
    g.add_edge(START, "gather")
    g.add_conditional_edges("gather", route, ["analyst", "save", END])
    g.add_edge("analyst", "save")
    g.add_edge("save", END)
    return g.compile()


def run_digest(conn, project: dict, *, since: datetime | None = None, llm=DEFAULT, store: bool = True,
               force: bool = False, retry_delay: float = 1.0) -> dict | None:
    """The digest for a workspace since `since` (default: the last digest). None when there is nothing new."""
    llm = chat_model("analyst") if llm is DEFAULT else llm
    out = build(llm, retry_delay=retry_delay).invoke({}, config={"configurable": {
        "conn": conn, "project": project, "since": since or digests.since_last(conn, project["id"]),
        "store": store, "force": force}})
    return out.get("saved")


def run_job(conn, job: dict) -> dict:
    """Worker handler for 'compare_project'."""
    project = conn.execute("select * from projects where id=%s", (int(job["payload"]["project_id"]),)).fetchone()
    if project is None:
        return {"status": "skipped"}
    d = run_digest(conn, project)
    return {"status": "saved" if d else "nothing new", "source": (d or {}).get("source")}
