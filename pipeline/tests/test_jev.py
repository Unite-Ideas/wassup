import json

import httpx
import pytest

from wassup.triage.decider import StoryContext
from wassup.triage.jev import JevDecider, _score_0_to_5


def ctx(title, titles=()):
    return StoryContext(id=1, title=title, item_titles=list(titles) or [title], summaries=[], urls=["https://e.com/1"],
                        themes=[], countries=["UA"], item_count=3, source_count=3, country_count=1, tiers=["B"])


def jev_reply(desk="russia_ukraine", desk_conf=0.9, excluded="none", larger=0.9, levels=None):
    levels = levels or {"0": 0.0, "1": 0.05, "2": 0.15, "3": 0.5, "4": 0.25, "5": 0.05}
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 1200, "output_tokens": 30}, "answers": {
        "desk": {"type": "choice", "choice": desk, "probabilities": {desk: desk_conf}, "confidence": desk_conf},
        "excluded": {"type": "choice", "choice": excluded, "probabilities": {excluded: 0.95}, "confidence": 0.95},
        "larger_story": {"type": "noul", "noul": larger},
        "significance": {"type": "score", "score": 3.1, "legend": {}, "probabilities": levels, "confidence": 0.6},
        "primary_release": {"type": "noul", "noul": 0.05},
    }}


def test_score_mapping_handles_zero_and_one_based_levels():
    assert _score_0_to_5({"probabilities": {"0": 1.0, "5": 0.0}}) == 0
    assert _score_0_to_5({"probabilities": {"1": 0.0, "6": 1.0}}) == 5
    assert round(_score_0_to_5({"probabilities": {"0": 0.5, "5": 0.5}}), 2) == 2.5


@pytest.mark.usefixtures("database")
def test_jev_decision_request_and_usage(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=jev_reply())

    d = JevDecider(api_key="ts_test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = d.decide(ctx("Russian drones hit Kyiv"))
    assert seen["auth"] == "Bearer ts_test"
    body = seen["body"]
    assert body["model"] == "jev-latest" and body["state"]["headline"] == "Russian drones hit Kyiv"
    assert set(body["questions"]) == {"desk", "excluded", "larger_story", "significance", "primary_release"}
    assert "none" in body["questions"]["desk"]["criteria"] and "russia_ukraine" in body["questions"]["desk"]["criteria"]
    assert len(body["questions"]["significance"]["criteria"]) == 6
    assert out.routed and out.desk == "russia_ukraine" and out.backend == "jev" and 2.5 < out.significance < 3.5

    from wassup.triage.jev import spent_today
    assert spent_today() > 0  # 1200 tokens recorded


def test_jev_celebrity_needs_larger_story(monkeypatch):
    monkeypatch.setattr("wassup.triage.jev.record_usage", lambda *a, **k: None)
    tr = httpx.MockTransport(lambda r: httpx.Response(200, json=jev_reply(desk="gov_releases", excluded="celebrity_entertainment", larger=0.2)))
    d = JevDecider(api_key="k", client=httpx.Client(transport=tr))
    out = d.decide(ctx("Actor attends gala"))
    assert not out.routed and out.excluded_reason == "celebrity_entertainment"
    tr2 = httpx.MockTransport(lambda r: httpx.Response(200, json=jev_reply(desk="gov_releases", excluded="celebrity_entertainment", larger=0.85)))
    assert JevDecider(api_key="k", client=httpx.Client(transport=tr2)).decide(ctx("Actor named in Epstein files")).routed


def test_jev_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr("wassup.triage.jev.record_usage", lambda *a, **k: None)
    monkeypatch.setattr("wassup.triage.jev.time.sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"}) if len(calls) < 3 else httpx.Response(200, json=jev_reply())

    assert JevDecider(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))).decide(ctx("x")).routed
    assert len(calls) == 3


def test_hybrid_falls_through_when_jev_unsure(monkeypatch):
    from wassup.triage import HybridDecider
    from wassup.triage.decider import Decision
    from wassup.triage.rules import RulesDecider

    class Fake:
        def __init__(self, name, conf, desk):
            self.name, self.conf, self.desk = name, conf, desk

        def available(self):
            return True

        def decide(self, c):
            return Decision(desk=self.desk, routed=True, excluded_reason=None, significance=3, relevance=0.8,
                            confidence=self.conf, backend=self.name, notes={})

    unsure = ctx("Parliament holds a session on many matters", titles=["Parliament holds a session", "Officials meet in capital"])
    h = HybridDecider(RulesDecider(), [Fake("jev", 0.3, "us_politics"), Fake("ollama", 0.8, "world_watch")])
    assert h.decide(unsure).backend == "hybrid:ollama"
    h2 = HybridDecider(RulesDecider(), [Fake("jev", 0.9, "us_politics"), Fake("ollama", 0.8, "world_watch")])
    assert h2.decide(unsure).backend == "hybrid:jev"
