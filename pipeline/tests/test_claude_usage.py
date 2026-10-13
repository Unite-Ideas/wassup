"""Claude usage: tokens per agent per hour from Paperclip, and the subscription meters."""
from datetime import datetime, timedelta, timezone

import pytest


class FakePaperclip:
    """One Editor in Chief run of 1,000 tokens every hour; a desk agent with nothing."""

    def __init__(self, now):
        self.now, self.calls, self.meters_ok = now, 0, True

    def costs_by_agent(self, company, start, end):
        self.calls += 1
        s = datetime.fromisoformat(start)
        if s > self.now:
            return []
        return [{"agentId": "eic", "agentName": "Editor in Chief", "inputTokens": 100, "cachedInputTokens": 800, "outputTokens": 100,
                 "costCents": 0, "apiRunCount": 0, "subscriptionRunCount": 1},
                {"agentId": "desk", "agentName": "Migration desk", "inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0,
                 "costCents": 0, "apiRunCount": 0, "subscriptionRunCount": 0}]

    def quota_windows(self, company):
        if not self.meters_ok:
            return [{"provider": "anthropic", "ok": False, "error": "anthropic usage api returned 403", "windows": []}]
        return [{"provider": "anthropic", "ok": True, "windows": [
            {"label": "Current session", "usedPercent": 42, "resetsAt": (self.now + timedelta(hours=2)).isoformat(), "valueLabel": None},
            {"label": "Current week (all models)", "usedPercent": 17, "resetsAt": None, "valueLabel": None}]}]


@pytest.mark.usefixtures("database")
def test_hourly_and_daily_claude_usage():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.db import kv_set
    from wassup.newsroom.usage import run_usage

    now = datetime.now(timezone.utc)
    pc = FakePaperclip(now)
    with db.connect() as conn:
        assert run_usage(conn, pc=pc) == 0  # not connected to Paperclip: nothing to do
        kv_set(conn, "paperclip", {"board_token": "t", "company_id": "c"})
        conn.commit()
        assert run_usage(conn, pc=pc, now=now) == 169  # a week back plus this hour; the idle desk is left out
        first = pc.calls
        pc.meters_ok = True
        run_usage(conn, pc=pc, now=now)
        assert pc.calls - first == 2  # later runs ask only for the last two hours again
        assert conn.execute("SELECT count(*) AS n FROM claude_usage").fetchone()["n"] == 169
    client = TestClient(app)
    s = client.get("/api/newsroom/claude-usage?hours=24").json()
    assert len(s["hourly"]) == 24 and s["hourly"][-1]["tokens"] == 1000
    assert sum(d["tokens"] for d in s["daily"]) >= 7 * 24 * 1000
    eic = s["agents"][0]
    assert eic["agent_name"] == "Editor in Chief" and eic["day"] == 24 * 1000 and eic["day_runs"] == 24
    assert s["meters"]["ok"] and s["meters"]["windows"][0]["usedPercent"] == 42
    # When the meters cannot be read, the reason is shown and tokens still work.
    pc.meters_ok = False
    with db.connect() as conn:
        run_usage(conn, pc=pc, now=now)
    s = client.get("/api/newsroom/claude-usage").json()
    assert not s["meters"]["ok"] and "403" in s["meters"]["error"] and s["hourly"]
