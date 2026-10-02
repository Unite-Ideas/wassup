"""Newsroom: desk check ins, questions, standups, escalation and surge agents, with Paperclip
replaced by a recorder (the real API was exercised by hand against Paperclip 2026.1001.0)."""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("database")


class FakePaperclip:
    """Records calls and returns the shapes the real API returns."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.ids = itertools.count(1)

    def __call__(self, pc, method, path, json=None, run_id=None, params=None):
        self.calls.append((method, path, json))
        if path.endswith("/issues") and method == "POST":
            return {"id": f"issue-{next(self.ids)}", "identifier": "WAS-1"}
        if path.endswith("/agents") and method == "POST":
            return {"id": f"agent-{next(self.ids)}", "name": json["name"]}
        if path.endswith("/keys"):
            return {"token": "pcp_test"}
        if path.endswith("/routines") and method == "POST":
            return {"id": f"routine-{next(self.ids)}"}
        return {}

    def find(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


@pytest.fixture
def newsroom(monkeypatch):
    from wassup import db
    from wassup.newsroom import paperclip, store

    fake = FakePaperclip()
    monkeypatch.setattr(paperclip.Paperclip, "_req", lambda self, *a, **k: fake(self, *a, **k))
    monkeypatch.setenv("NEWSROOM_LLM", "extractive")
    with db.connect() as conn:
        for t in ("newsroom_events", "briefs", "follows", "standup_reports", "standups", "escalations", "newsroom_agents"):
            conn.execute(f"DELETE FROM {t}")
        paperclip.save_config(conn, board_token="pcp_board", company_id="co-1", project_id="proj-1",
                              eic_agent_id="agent-eic", wassup_token="secret")
        store.upsert_agent(conn, "eic", "eic", "Editor in Chief", paperclip_agent_id="agent-eic", paperclip_api_key="k", status="active")
        store.upsert_agent(conn, "desk:russia_ukraine", "desk", "Russia and Ukraine Desk", desk="russia_ukraine",
                           paperclip_agent_id="agent-ru", paperclip_api_key="pcp_ru", status="active")
        store.upsert_agent(conn, "desk:us_politics", "desk", "US Politics and Congress Desk", desk="us_politics",
                           paperclip_agent_id="agent-us", paperclip_api_key="pcp_us", status="active")
        conn.commit()
        yield conn, fake


def _stories(conn):
    """Two routed stories on different desks, plus their articles."""
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.geo import gazetteer
    from wassup.triage import run_triage

    now = datetime.now(timezone.utc)
    g = gazetteer()
    sid = ensure_source(conn, "nr_test", "NR test", "rss")
    items = [RawItem(url=f"https://nr.example/{i}", title=t, published_at=now - timedelta(minutes=i), places=[g.by_name(p)],
                     outlet=f"o{i}.com", outlet_tier="B", meta={"themes": ["ARMEDCONFLICT"]})
             for i, (t, p) in enumerate([
                 ("Russian missiles strike Kyiv energy grid as Zelensky urges air defense", "Kyiv"),
                 ("Russian missiles strike Kyiv energy grid, officials say", "Kyiv"),
                 ("Senate debates Ukraine air defense aid package in Congress", "Washington"),
                 ("Senate debates Ukraine air defense aid package, senators say", "Washington"),
             ])]
    store_items(conn, sid, items)
    conn.commit()
    process_new(conn)
    run_triage(conn)
    return {r["desk"]: r["id"] for r in conn.execute(
        "SELECT DISTINCT ON (s.desk) s.id, s.desk FROM stories s JOIN items i ON i.story_id = s.id WHERE i.url LIKE 'https://nr.example/%' ORDER BY s.desk, s.item_count DESC")}


def _heartbeat(conn, agent_id, title, description="", issue="issue-run"):
    from wassup.newsroom.desk import handle_heartbeat

    return handle_heartbeat(conn, {"agentId": agent_id, "runId": "run-1", "context": {
        "issueId": issue, "wakeReason": "issue_assigned", "paperclipIssue": {"title": title, "description": description}}})


def test_desk_check_in_writes_briefs_links_and_closes_its_task(newsroom):
    conn, fake = newsroom
    ids = _stories(conn)
    assert {"russia_ukraine", "us_politics"} <= set(ids)
    out = _heartbeat(conn, "agent-ru", "Check in: Russia and Ukraine")
    assert out["ok"]
    briefs = conn.execute("SELECT story_id, agent_key FROM briefs WHERE kind = 'story'").fetchall()
    assert any(b["story_id"] == ids["russia_ukraine"] and b["agent_key"] == "desk:russia_ukraine" for b in briefs)
    assert conn.execute("SELECT count(*) n FROM follows WHERE agent_key = 'desk:russia_ukraine' AND active").fetchone()["n"] >= 1
    link = conn.execute("SELECT created_by, evidence FROM story_links WHERE created_by = 'agent:desk:russia_ukraine'").fetchone()
    assert link and link["evidence"]["reason"]
    closes = fake.find("PATCH", "/issues/issue-run")
    assert closes and closes[-1][2]["status"] == "done" and "Russia and Ukraine Desk" in closes[-1][2]["comment"]
    agent = conn.execute("SELECT last_run_at, last_summary FROM newsroom_agents WHERE key = 'desk:russia_ukraine'").fetchone()
    assert agent["last_run_at"] and agent["last_summary"]


def test_question_from_the_editor_is_answered_and_closed(newsroom):
    conn, fake = newsroom
    _stories(conn)
    _heartbeat(conn, "agent-us", "What is Congress doing about Ukraine air defense?", issue="issue-q")
    answer = conn.execute("SELECT body FROM briefs WHERE kind = 'answer'").fetchone()
    assert answer and "Senate" in answer["body"]
    assert fake.find("PATCH", "/issues/issue-q")[-1][2]["status"] == "done"


def test_standup_collects_every_desk_then_hands_off(newsroom):
    from wassup.newsroom.manager import call_standup, hand_off_standups

    conn, fake = newsroom
    r = call_standup(conn, "eic", "air defense")
    assert r["desks_woken"] == 2
    titles = [c[2]["title"] for c in fake.find("POST", "/issues")]
    assert len(set(titles)) == 2  # Paperclip merges identical issues, so each desk's task is distinct
    assert call_standup(conn, "eic")["already_open"]
    _heartbeat(conn, "agent-ru", titles[0])
    assert hand_off_standups(conn) == 0  # one desk still to report
    _heartbeat(conn, "agent-us", titles[1])
    assert hand_off_standups(conn) == 1
    handoff = fake.find("POST", "/issues")[-1][2]
    assert handoff["assigneeAgentId"] == "agent-eic" and "write it up" in handoff["title"]
    assert "Russia and Ukraine Desk" in handoff["description"] and "US Politics and Congress Desk" in handoff["description"]


def test_breaking_story_escalated_once_within_daily_cap(newsroom):
    from wassup.newsroom.manager import escalate

    conn, fake = newsroom
    ids = _stories(conn)
    conn.execute("UPDATE stories SET breaking = true, velocity = 20, significance = 4 WHERE id = %s", (ids["russia_ukraine"],))
    conn.commit()
    assert escalate(conn) == 1
    issue = fake.find("POST", "/issues")[-1][2]
    assert issue["assigneeAgentId"] == "agent-eic" and issue["title"].startswith("Breaking:")
    assert escalate(conn) == 0  # already raised


def test_surge_hire_caps_and_retire(newsroom, monkeypatch):
    from wassup.newsroom import surge

    conn, fake = newsroom
    ids = _stories(conn)
    out = surge.request_surge(conn, ids["russia_ukraine"], "major strike", "eic")
    assert out["ok"] and not out.get("already")
    hired = fake.find("POST", "/companies/co-1/agents")[-1][2]
    assert hired["adapterType"] == "http" and hired["reportsTo"] == "agent-eic"
    assert hired["adapterConfig"]["headers"]["x-wassup-token"] == "secret"
    assert fake.find("POST", "/run"), "first check in should start right away"
    assert surge.request_surge(conn, ids["russia_ukraine"], "again", "eic")["already"]

    monkeypatch.setattr(surge, "load_yaml", lambda name: {"surge": {"max_active": 1}, "limits": {"max_agents": 20}})
    with pytest.raises(surge.SurgeRefused):
        surge.request_surge(conn, ids["us_politics"], "another", "eic")

    key = f"surge:{ids['russia_ukraine']}"
    surge.retire(conn, key, "cooled")
    assert conn.execute("SELECT status FROM newsroom_agents WHERE key = %s", (key,)).fetchone()["status"] == "retired"
    assert fake.find("POST", "/terminate") and fake.find("PATCH", "/routines/" + str(conn.execute(
        "SELECT meta->>'routine_id' r FROM newsroom_agents WHERE key = %s", (key,)).fetchone()["r"]))


def test_surge_retires_itself_when_story_goes_quiet(newsroom):
    from wassup.newsroom import surge
    from wassup.newsroom.desk import handle_heartbeat

    conn, fake = newsroom
    ids = _stories(conn)
    surge.request_surge(conn, ids["russia_ukraine"], "major strike", "eic")
    agent_id = conn.execute("SELECT paperclip_agent_id FROM newsroom_agents WHERE kind = 'surge'").fetchone()["paperclip_agent_id"]
    for _ in range(4):
        handle_heartbeat(conn, {"agentId": agent_id, "runId": "r", "context": {
            "issueId": "i", "paperclipIssue": {"title": "Check in: Surge"}}})
    assert conn.execute("SELECT status FROM newsroom_agents WHERE kind = 'surge'").fetchone()["status"] == "retired"


def test_agent_api_requires_token(newsroom):
    from fastapi.testclient import TestClient
    from wassup.api import app

    conn, _ = newsroom
    ids = _stories(conn)
    c = TestClient(app)
    assert c.post("/api/newsroom/heartbeat", json={}).status_code == 401
    assert c.post("/api/newsroom/briefs", json={"kind": "daily", "body": "x"}).status_code == 401
    ok = c.post("/api/newsroom/briefs", json={"kind": "daily", "title": "Today", "body": "**All quiet**"}, headers={"x-wassup-token": "secret"})
    assert ok.status_code == 200
    link = c.post("/api/newsroom/links", headers={"x-wassup-token": "secret"},
                  json={"a": ids["us_politics"], "b": ids["russia_ukraine"], "relation": "responds_to", "reason": "aid answers the strikes"})
    assert link.status_code == 200
    detail = c.get(f"/api/stories/{ids['russia_ukraine']}").json()
    assert any(n["created_by"] == "agent:eic" and n["evidence"]["reason"] == "aid answers the strikes" for n in detail["links"])
    ov = c.get("/api/newsroom/overview").json()
    assert ov["agent_links"] and {d["desk"] for d in ov["desks"]} == {"russia_ukraine", "us_politics"}
    assert c.get("/api/newsroom/agents").json()[0]["key"]
    assert conn.execute("SELECT last_run_at FROM newsroom_agents WHERE key = 'eic'").fetchone()["last_run_at"]
