import json

import httpx

from wassup.triage.decider import StoryContext
from wassup.triage.ollama import OllamaDecider
from wassup.triage.rules import RulesDecider


def ctx(title, titles=(), urls=(), themes=(), countries=(), items=3, sources=3, ncountries=1, tiers=("B",)):
    return StoryContext(id=1, title=title, item_titles=list(titles) or [title], summaries=[], urls=list(urls) or ["https://e.com/news/1"],
                        themes=list(themes), countries=list(countries), item_count=items, source_count=sources,
                        country_count=ncountries, tiers=list(tiers))


def test_war_story_routed_to_desk():
    d = RulesDecider().decide(ctx("Russian drones hit Kyiv as Zelensky urges more air defense", themes=["ARMEDCONFLICT"], countries=["UA"]))
    assert d.routed and d.desk == "russia_ukraine" and d.significance > 2


def test_sports_goes_to_cold_storage():
    d = RulesDecider().decide(ctx("Premier League: striker scores twice as champions win", urls=["https://e.com/sport/football/1"]))
    assert not d.routed and d.excluded_reason == "sports"


def test_celebrity_in_international_scandal_still_routed():
    d = RulesDecider().decide(ctx(
        "Hollywood actor named in Epstein files as Senate subpoenas flight logs",
        titles=["Hollywood actor named in Epstein files", "Senate committee subpoenas Epstein flight logs, documents released"]))
    assert d.routed and d.desk == "gov_releases"
    assert d.notes["excluded"] == "celebrity_entertainment"


def test_unrelated_story_is_cold():
    d = RulesDecider().decide(ctx("Local council approves new bike lanes"))
    assert not d.routed and d.excluded_reason == "no_desk_match" and d.desk is None


def test_themes_alone_do_not_assign_a_desk():
    # LEGISLATION fits any parliament; without a keyword or location it should not pull in the US desk.
    d = RulesDecider().decide(ctx("Parliament debates pension reform", themes=["LEGISLATION", "ELECTION"]))
    assert d.desk != "us_politics" or not d.routed


def test_ollama_decider_parses_structured_answer():
    answer = {"desk": "iran_mideast", "excluded_category": "none", "part_of_larger_story": False,
              "significance": 4, "primary_source_release": False, "confidence": 0.82}
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        body = json.loads(request.content)
        seen["format"] = body["format"]
        return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})

    dec = OllamaDecider(url="http://ollama", model="qwen3:8b")
    dec.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert dec.available()
    d = dec.decide(ctx("Iran test fires missile near Strait of Hormuz"))
    assert d.routed and d.desk == "iran_mideast" and d.significance == 4 and d.confidence == 0.82
    assert "iran_mideast" in seen["format"]["properties"]["desk"]["enum"]


def test_ollama_decider_respects_larger_story_override():
    answer = {"desk": "us_politics", "excluded_category": "sports", "part_of_larger_story": True,
              "significance": 3, "primary_source_release": False, "confidence": 0.7}
    dec = OllamaDecider(url="http://ollama", model="m")
    dec.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {"content": json.dumps(answer)}})))
    assert dec.decide(ctx("NFL team owner testifies before Senate on foreign funding")).routed
