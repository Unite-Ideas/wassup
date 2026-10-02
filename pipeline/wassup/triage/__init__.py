"""Triage: decides which desk owns each story, how significant it is, and whether it goes to
cold storage. Runs per story (not per article), and again when a story grows, so the number
of decisions stays small even when thousands of articles arrive."""
from __future__ import annotations

import logging
import time

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from ..config import settings
from ..embed import as_array
from .decider import Decider, Decision, StoryContext
from .ollama import OllamaDecider
from .rules import RulesDecider

log = logging.getLogger(__name__)


class HybridDecider(Decider):
    """Rules first. Ask the local LLM only when the rules are unsure and the story has more
    than one article, which is where a second opinion is worth the GPU time."""

    name = "hybrid"

    def __init__(self, rules: RulesDecider, llm: OllamaDecider, min_items: int = 2, unsure_below: float = 0.6):
        self.rules, self.llm = rules, llm
        self.min_items, self.unsure_below = min_items, unsure_below

    def decide(self, ctx: StoryContext) -> Decision:
        d = self.rules.decide(ctx)
        if d.confidence >= self.unsure_below or ctx.item_count < self.min_items or not self.llm.available():
            return d
        try:
            l = self.llm.decide(ctx)
        except Exception as e:
            log.warning("llm triage failed for story %s: %s", ctx.id, e)
            return d
        l.significance = round((l.significance + d.significance) / 2, 2)
        l.relevance = d.relevance if l.routed and d.routed else l.relevance
        l.backend = "hybrid:llm"
        l.notes = {**d.notes, **l.notes}
        return l


def get_decider() -> Decider:
    backend = settings().triage_backend
    rules = RulesDecider()
    if backend == "rules":
        return rules
    llm = OllamaDecider()
    if backend == "ollama":
        return HybridDecider(rules, llm, min_items=1, unsure_below=1.01)
    return HybridDecider(rules, llm)


def _profile(conn: psycopg.Connection) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Mean centroid of stories you liked and of stories you disliked."""
    out = []
    for v in (1, -1):
        rows = conn.execute(
            "SELECT s.centroid FROM feedback f JOIN stories s ON s.id = f.story_id WHERE f.value = %s "
            "ORDER BY f.created_at DESC LIMIT 500", (v,)).fetchall()
        if rows:
            m = np.mean([as_array(r["centroid"]) for r in rows], axis=0)
            out.append(m / (np.linalg.norm(m) or 1))
        else:
            out.append(None)
    return out[0], out[1]


def learned_adjustment(centroid: np.ndarray, liked: np.ndarray | None, disliked: np.ndarray | None) -> float:
    adj = 0.0
    if liked is not None:
        adj += float(centroid @ liked)
    if disliked is not None:
        adj -= float(centroid @ disliked)
    if liked is not None and disliked is not None:
        adj /= 2
    return round(0.6 * adj, 3) if (liked is not None or disliked is not None) else 0.0


NEEDS_TRIAGE = """
    SELECT id, title, centroid, item_count, source_count, country_count FROM stories
    WHERE triaged_item_count = 0
       OR item_count >= triaged_item_count * 2
       OR item_count >= triaged_item_count + 10
    ORDER BY (triaged_item_count = 0) DESC, item_count DESC
    LIMIT %s"""


def _contexts(conn: psycopg.Connection, stories: list[dict]) -> list[StoryContext]:
    ids = [s["id"] for s in stories]
    items: dict[int, list[dict]] = {i: [] for i in ids}
    for r in conn.execute(
        """SELECT * FROM (
             SELECT i.story_id, i.title, i.summary, i.url, i.meta, coalesce(i.outlet_tier, s.trust_tier) tier,
                    row_number() OVER (PARTITION BY i.story_id ORDER BY i.published_at DESC) rn
             FROM items i JOIN sources s ON s.id = i.source_id WHERE i.story_id = ANY(%s)) x
           WHERE rn <= 12""", (ids,)):
        items[r["story_id"]].append(r)
    countries: dict[int, list[str]] = {i: [] for i in ids}
    for r in conn.execute(
        """SELECT sp.story_id, p.country FROM story_places sp JOIN places p ON p.id = sp.place_id
           WHERE sp.story_id = ANY(%s) AND p.country IS NOT NULL""", (ids,)):
        countries[r["story_id"]].append(r["country"])
    out = []
    for s in stories:
        its = items[s["id"]]
        out.append(StoryContext(
            id=s["id"], title=s["title"],
            item_titles=list(dict.fromkeys(i["title"] for i in its)),
            summaries=[i["summary"][:400] for i in its if i["summary"]][:5],
            urls=[i["url"] for i in its],
            themes=sorted({t for i in its for t in (i["meta"] or {}).get("themes", [])}),
            countries=sorted(set(countries[s["id"]])),
            item_count=s["item_count"], source_count=s["source_count"], country_count=s["country_count"],
            tiers=[i["tier"] for i in its],
        ))
    return out


def run_triage(conn: psycopg.Connection, limit: int = 500, max_seconds: float = 90) -> int:
    decider = get_decider()
    liked, disliked = _profile(conn)
    started, done = time.time(), 0
    while time.time() - started < max_seconds:
        stories = conn.execute(NEEDS_TRIAGE, (limit,)).fetchall()
        if not stories:
            break
        feedback = {r["story_id"]: r["value"] for r in conn.execute(
            "SELECT DISTINCT ON (story_id) story_id, value FROM feedback WHERE story_id = ANY(%s) ORDER BY story_id, created_at DESC",
            ([s["id"] for s in stories],))}
        by_id = {s["id"]: s for s in stories}
        for ctx in _contexts(conn, stories):
            d = decider.decide(ctx)
            adj = learned_adjustment(as_array(by_id[ctx.id]["centroid"]), liked, disliked)
            d.relevance = round(max(0.0, min(1.0, d.relevance + adj)), 3)
            d.significance = round(max(0.0, min(5.0, d.significance + adj)), 2)
            if d.routed and adj <= -0.25 and d.significance < 4:
                d.routed, d.excluded_reason = False, "learned_dislike"
            fb = feedback.get(ctx.id)
            if fb == -1:
                d.routed, d.excluded_reason = False, "you_dismissed"
            elif fb == 1:
                d.routed, d.excluded_reason = True, None
                d.desk = d.desk or "world_watch"
            conn.execute(
                """UPDATE stories SET desk = %s, routed = %s, excluded_reason = %s, significance = %s, relevance = %s,
                          triage = %s, triaged_item_count = item_count, updated_at = now() WHERE id = %s""",
                (d.desk, d.routed, d.excluded_reason, d.significance, d.relevance,
                 Jsonb({"backend": d.backend, "confidence": d.confidence, "learned": adj, **d.notes}), ctx.id),
            )
            done += 1
        conn.commit()
    if done:
        log.info("triaged %d stories", done)
    return done
