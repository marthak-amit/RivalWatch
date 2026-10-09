"""The analyst digest: facts from code, wording from the (fake) model, checked before it is stored."""
from datetime import UTC, datetime

from competitor_moves.agents.compare.graph import run_digest
from competitor_moves.agents.compare.prompts import Digest
from competitor_moves.services import change_detection as cd
from competitor_moves.workers import worker
from tests.integration.test_comparison import workspace


class FakeAnalyst:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return Digest(**self.reply(prompt)) if callable(self.reply) else Digest(**self.reply)


def setup(client, conn, make_site):
    ours, us, _uk = workspace(client, conn, make_site)
    cd.store_events(conn, us, None, [{"type": "price_drop", "category": "Engagement Rings", "before": {"price": 1000.0},
                                      "after": {"price": 900.0, "pct": -10.0, "currency": "USD"},
                                      "occurred_at": datetime.now(UTC)}],
                    product={"title": "Ring X", "url": "https://x/1", "currency": "USD"})
    project = conn.execute("select p.* from projects p join sites s on s.project_id = p.id where s.id=%s", (ours,)).fetchone()
    return project, us


def ids(prompt, prefix):
    return [line.split(" | ")[0] for line in prompt.splitlines() if line.startswith(prefix)]


def test_actions_are_checked_against_their_evidence_then_topped_up(client, conn, make_site):
    project, _ = setup(client, conn, make_site)

    def reply(prompt):
        e, p = ids(prompt, "e:")[0], ids(prompt, "p:")[0]
        return {"summary": "BrightUS cut Ring X to 900 and prices lab-grown engagement rings about 20.8% below ours.",
                "actions": [
                    {"title": "Review lab-grown engagement ring prices against BrightUS", "priority": "high", "evidence": [p],
                     "why": "Their median is 950 against our 1200, a gap of 20.8%."},
                    {"title": "Answer the Ring X cut", "priority": "high", "evidence": [e], "why": "BrightUS dropped Ring X to 900."},
                    {"title": "Invented action", "priority": "low", "evidence": ["e:nope"], "why": "No real evidence."},
                    {"title": "Exaggerated action", "priority": "medium", "evidence": [p], "why": "They are 45% cheaper."}]}
    fake = FakeAnalyst(reply)
    d = run_digest(conn, project, llm=fake, retry_delay=0)
    assert "<positioning>" in fake.prompts[0] and "engagement ring | ours: 3, median 1200.0 USD" in fake.prompts[0]
    titles = [a["title"] for a in d["actions"]]
    assert titles[:2] == ["Review lab-grown engagement ring prices against BrightUS", "Answer the Ring X cut"]
    assert "Invented action" not in titles and "Exaggerated action" not in titles  # unknown evidence / a number not in it
    assert len(d["actions"]) == 3 and d["actions"][2]["evidence"] == [] and d["source"] == "gemini+rules"
    row = conn.execute("select * from digests where id=%s", (d["id"],)).fetchone()
    assert row["prompt_version"] == "compare-v1" and row["change_count"] == 1 and any(k.startswith("p:") for k in row["evidence"])


def test_nothing_new_means_no_model_call_and_failures_fall_back_to_rules(client, conn, make_site):
    project, _ = setup(client, conn, make_site)
    first = run_digest(conn, project, llm=FakeAnalyst(RuntimeError("quota")), retry_delay=0)
    assert first["source"] == "rules" and len(first["actions"]) >= 3
    again = FakeAnalyst({"summary": "x", "actions": []})
    assert run_digest(conn, project, llm=again, retry_delay=0) is None and again.prompts == []


def test_the_last_crawl_of_a_round_queues_the_digest_and_the_dashboard_shows_it(client, conn, make_site):
    _project, us = setup(client, conn, make_site)
    conn.execute("insert into jobs(type, payload, status) values ('crawl_site', %s, 'done')", (f'{{"site_id": {us}}}',))
    from competitor_moves.services import digests
    assert digests.maybe_queue(conn, us) is True
    assert digests.maybe_queue(conn, us) is False  # already queued
    while worker.run_once(conn):
        pass
    state = client.get("/api/state").json()
    assert state["digest"]["source"] == "rules" and state["digest"]["changeCount"] == 1  # no Gemini key in tests
    assert {"title", "why", "priority", "evidence"} <= set(state["digest"]["actions"][0])
