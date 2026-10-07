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
from ..db import lock_stories
from ..embed import as_array
from .decider import Decider, Decision, StoryContext
from .ollama import OllamaDecider
from .rules import RulesDecider

log = logging.getLogger(__name__)


class HybridDecider(Decider):
    """Rules first. When the rules are unsure about a story with more than one article, ask for
    a second opinion: Jev when it is configured (fast, cheap, calibrated), then the local LLM if
    Jev is unsure too or unavailable."""

    name = "hybrid"

    def __init__(self, rules: RulesDecider, opinions: list, min_items: int = 2, unsure_below: float = 0.6,
                 jev_trust_above: float = 0.55):
        self.rules, self.opinions = rules, opinions
        self.min_items, self.unsure_below, self.jev_trust_above = min_items, unsure_below, jev_trust_above

    def decide(self, ctx: StoryContext) -> Decision:
        d = self.rules.decide(ctx)
        if d.confidence >= self.unsure_below or ctx.item_count < self.min_items:
            return d
        for other in self.opinions:
            if not other.available():
                continue
            try:
                o = other.decide(ctx)
            except Exception as e:
                log.warning("%s triage failed for story %s: %s", other.name, ctx.id, e)
                continue
            if other.name == "jev" and o.confidence < self.jev_trust_above and other is not self.opinions[-1]:
                d.notes["jev_unsure"] = o.notes.get("jev")
                continue  # let the next opinion weigh in
            o.significance = round((o.significance + d.significance) / 2, 2)
            o.relevance = d.relevance if o.routed and d.routed else o.relevance
            o.backend = f"hybrid:{other.name}"
            o.notes = {**d.notes, **o.notes}
            return o
        return d


def get_decider() -> Decider:
    from .jev import JevDecider

    backend = settings().triage_backend
    rules = RulesDecider()
    if backend == "rules":
        return rules
    jev, llm = JevDecider(), OllamaDecider()
    if backend == "jev":
        return HybridDecider(rules, [jev, llm], min_items=1, unsure_below=1.01)
    if backend == "ollama":
        return HybridDecider(rules, [llm], min_items=1, unsure_below=1.01)
    return HybridDecider(rules, [jev, llm])


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
    SELECT id, coalesce(title_en, title) AS title, centroid, item_count, source_count, country_count FROM stories
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
             SELECT i.story_id, coalesce(i.title_en, i.title) AS title, i.summary, i.url, i.meta, coalesce(i.outlet_tier, s.trust_tier) tier,
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
        writes = []
        # Decide everything first (this can take a while when Jev or the model is asked), then
        # write in one short transaction, locking stories in id order like the other lanes do.
        for ctx in _contexts(conn, stories):
            d = decider.decide(ctx)
            c = by_id[ctx.id]["centroid"]  # cleared on stories older than the retention window
            adj = learned_adjustment(as_array(c), liked, disliked) if c is not None else 0.0
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
            writes.append((d.desk, d.routed, d.excluded_reason, d.significance, d.significance, d.relevance,
                           Jsonb({"backend": d.backend, "confidence": d.confidence, "learned": adj, **d.notes}), ctx.id))
        conn.commit()  # end the read transaction before taking locks
        writes.sort(key=lambda w: w[-1])
        lock_stories(conn, [w[-1] for w in writes])
        with conn.cursor() as cur:
            cur.executemany(
                """UPDATE stories SET desk = %s, routed = %s, excluded_reason = %s, importance = %s,
                          significance = wassup_significance(%s, source_count, country_count), relevance = %s,
                          triage = %s, triaged_item_count = item_count, updated_at = now() WHERE id = %s""", writes)
        conn.commit()
        done += len(writes)
    if done:
        log.info("triaged %d stories", done)
    return done
