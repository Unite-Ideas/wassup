"""Triage with Jev, TypeSafe AI's System One model: typed answers with calibrated
probabilities, about a hundred milliseconds and a fraction of a cent per story.

One request asks every triage question about a story at once (they are evaluated in
parallel against the same state). Spend is recorded per day and capped by
JEV_DAILY_BUDGET_USD; when the cap is reached Jev is skipped until tomorrow.

API: https://docs.typesafe.ai/api
"""
from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from .. import db
from ..config import load_yaml, settings
from .decider import Decider, Decision, StoryContext

log = logging.getLogger(__name__)

URL = "https://api.typesafe.ai/v1/systemone"

SIGNIFICANCE_LEVELS = [
    "Trivial: local, routine, or human interest with no wider consequence",
    "Minor: a routine development in an ongoing story, of interest mainly to specialists",
    "Notable: a real development with national consequences in one country",
    "Significant: affects several countries, a government's policy, or an ongoing conflict",
    "Major: a large escalation, a major government decision, or a crisis with international consequences",
    "World changing: war breaking out or ending, a coup, a head of state removed, or an event the whole world reacts to",
]


class JevDecider(Decider):
    name = "jev"

    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None):
        s = settings()
        self.api_key = api_key if api_key is not None else s.typesafe_api_key
        self.model = model or s.jev_model
        self.client = client or httpx.Client(timeout=30)
        desks = load_yaml("desks.yaml").get("desks", [])
        self.desk_criteria = {d["key"]: f"{d['name']}. {d.get('description') or ''} Typical topics: {', '.join((d.get('keywords') or [])[:14])}".strip()
                              for d in desks}
        self.desk_criteria["none"] = "None of these desks: the story is outside every desk's beat."
        excluded = (load_yaml("interests.yaml").get("exclude") or {})
        self.excluded_criteria = {k: f"Mainly {k.replace('_', ' ')} news (for example: {', '.join((v.get('keywords') or [])[:8])})" for k, v in excluded.items()}
        self.excluded_criteria["none"] = "Not mainly any of these: it is news about politics, conflict, government, society, or the economy."

    def available(self) -> bool:
        return bool(self.api_key) and within_budget()

    def questions(self) -> dict:
        return {
            "desk": {"type": "choice", "instructions": "Which news desk should own this story?", "criteria": self.desk_criteria},
            "excluded": {"type": "choice", "instructions": "Is this story mainly one of these kinds of news the analyst does not follow?",
                         "criteria": self.excluded_criteria},
            "larger_story": {"type": "noul", "instructions":
                             "Is this story tied to a war, a government or legislative action, an international dispute, a scandal involving "
                             "officials, a migration crisis, or another event of geopolitical consequence?"},
            "significance": {"type": "score", "instructions": "How significant is what happened, for someone tracking world affairs? Judge the event itself, not how many outlets covered it.",
                             "criteria": SIGNIFICANCE_LEVELS},
            "primary_release": {"type": "noul", "instructions":
                                "Is this story about a government, court, or agency releasing documents or records (declassified files, "
                                "court filings, FOIA releases, official reports)?"},
        }

    def state(self, ctx: StoryContext) -> dict:
        return {
            "headline": ctx.title,
            "other_headlines": [t for t in ctx.item_titles if t != ctx.title][:10],
            "summaries": [s[:300] for s in ctx.summaries[:3]],
            "coverage": {"articles": ctx.item_count, "outlets": ctx.source_count, "countries": ctx.country_count},
            "locations": ctx.countries[:8],
        }

    def ask(self, ctx: StoryContext) -> dict:
        body = {"model": self.model, "state": self.state(ctx), "questions": self.questions()}
        for attempt in range(4):
            r = self.client.post(URL, json=body, headers={"authorization": f"Bearer {self.api_key}"})
            if r.status_code in (429, 529) and attempt < 3:
                time.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
                continue
            if r.status_code == 401:
                raise RuntimeError("Jev rejected the API key (401). Check TYPESAFE_API_KEY.")
            r.raise_for_status()
            out = r.json()
            usage = out.get("usage") or {}
            record_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            return out
        raise RuntimeError("Jev rate limited after retries")

    def decide(self, ctx: StoryContext) -> Decision:
        out = self.ask(ctx)
        a = out["answers"]
        desk = a["desk"]["choice"]
        excluded = a["excluded"]["choice"]
        larger = float(a["larger_story"]["noul"])
        sig = _score_0_to_5(a["significance"])
        desk = None if desk == "none" else desk
        excluded = None if excluded == "none" else excluded
        routed = desk is not None and (excluded is None or larger >= 0.5)
        confidence = min(float(a["desk"].get("confidence", 0)), float(a["excluded"].get("confidence", 1)))
        return Decision(
            desk=desk, routed=routed, excluded_reason=None if routed else (excluded or "no_desk_match"),
            significance=round(sig, 2), relevance=round(0.4 + 0.6 * larger, 3) if routed else 0.1,
            confidence=round(confidence, 3), backend=self.name,
            notes={"jev": {"model": out.get("model"), "desk": a["desk"].get("probabilities"), "excluded": excluded,
                           "larger_story": round(larger, 3), "primary_release": round(float(a["primary_release"]["noul"]), 3),
                           "significance": a["significance"].get("score")}},
        )


def _score_0_to_5(ans: dict) -> float:
    """Map a Score answer onto Wassup's 0 to 5 significance, whatever the level numbering."""
    probs = {int(k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
    if probs:
        lo, hi = min(probs), max(probs)
        mean = sum(k * p for k, p in probs.items())
        return 5 * (mean - lo) / ((hi - lo) or 1)
    return float(ans.get("score", 0))


def record_usage(input_tokens: int, output_tokens: int, provider: str = "jev") -> None:
    cost = input_tokens / 1_000_000 * settings().jev_price_per_mtok
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO api_usage (day, provider, requests, input_tokens, output_tokens, cost_usd) VALUES (%s, %s, 1, %s, %s, %s)
               ON CONFLICT (day, provider) DO UPDATE SET requests = api_usage.requests + 1,
                 input_tokens = api_usage.input_tokens + EXCLUDED.input_tokens,
                 output_tokens = api_usage.output_tokens + EXCLUDED.output_tokens, cost_usd = api_usage.cost_usd + EXCLUDED.cost_usd""",
            (date.today(), provider, input_tokens, output_tokens, cost))
        conn.commit()


def spent_today(provider: str = "jev") -> float:
    with db.connect() as conn:
        row = conn.execute("SELECT cost_usd FROM api_usage WHERE day = %s AND provider = %s", (date.today(), provider)).fetchone()
    return float(row["cost_usd"]) if row else 0.0


def within_budget() -> bool:
    ok = spent_today() < settings().jev_daily_budget_usd
    if not ok:
        log.info("Jev daily budget reached; using the local model until tomorrow")
    return ok
