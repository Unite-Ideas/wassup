"""A second look at which desk each story is on, by the local model.

Triage's rules decide most stories from keywords and GDELT's topic tags, and are often sure of
themselves when they should not be: a steel strike in Taranto tagged with a workers' theme lands
on Migration, a Seoul redevelopment story about "displaced" tenants too. So every story on a desk
is shown to the model once (again when it doubles in size), a dozen at a time: its headlines and
the desks' descriptions. The model says which desk it belongs on, or none, and which country it
is mainly about.

- A different desk moves the story there; "none" sends it to cold storage, marked so you can
  see why (include cold storage on the globe to find them).
- Stories the rules left in cold storage (matching no desk) are looked at too, so a story they
  missed is brought onto the right desk.
- The country feeds the place lists: a story mainly about the US is not listed as being about
  Italy because an Italian paper covered it. When the story's dot rests on weak evidence (only
  GDELT's tagger, or little of it) and is in another country, the dot moves to the story's
  strongest place in the right country, or to the country itself.
Triage keeps the review's desk until the story has doubled in size, then it is reviewed again.
Your own MORE and LESS in the story panel always win.
"""
from __future__ import annotations

import logging
import os
import time

import psycopg
from psycopg.types.json import Jsonb

from ..db import lock_stories
from .rules import load_desks

log = logging.getLogger(__name__)

BATCH = 12
PER_RUN = 60

PENDING = """
    SELECT s.id, coalesce(s.title_en, s.title) AS title, s.desk, s.routed, s.item_count, s.location_locked,
           s.location_source, s.location_confidence, p.country AS place_country
    FROM stories s LEFT JOIN places p ON p.id = s.primary_place_id
    WHERE s.last_seen > now() - interval '7 days'
      AND ((s.routed AND s.desk IS NOT NULL)
           OR (NOT s.routed AND s.excluded_reason = 'no_desk_match' AND s.item_count >= 2))
      AND (s.desk_reviewed_items IS NULL OR s.item_count >= 2 * s.desk_reviewed_items)
      AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.story_id = s.id)
    ORDER BY s.routed DESC, s.last_seen > now() - interval '1 day' DESC, s.significance DESC, s.item_count DESC LIMIT %s"""


def schema(keys: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"stories": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"},
            "desk": {"type": "string", "enum": keys + ["none"]},
            "country": {"type": "string"},
            "sure": {"type": "boolean"}},
            "required": ["n", "desk", "country", "sure"]}}},
        "required": ["stories"],
    }


def review_batch(llm, desks, stories: list[dict], headlines: dict[int, list[str]]) -> list[dict]:
    desk_lines = "\n".join(f"- {d.key}: {d.name}. {d.description}" for d in desks)
    story_lines = "\n".join(
        f"[{i + 1}] {s['title']}" + ("" if s.get("routed", True) else "  (currently on no desk)") + "".join(f"\n    also: {h}" for h in headlines.get(s["id"], [])[:2] if h != s["title"])
        for i, s in enumerate(stories))
    prompt = f"""You sort news stories onto the desks of a news intelligence team.

Desks:
{desk_lines}

Stories (headlines in English, numbered):
{story_lines}

For each story give:
- desk: the desk whose subject the story is actually about, or "none" if it is not about any desk's subject.
  Judge by what happened, not by words it shares with a desk (a labour strike is not migration because
  workers are mentioned; a local crime is not world_watch). world_watch is only for wars, coups, crises and
  major power moves not covered by another desk.
- country: the ISO 3166 two letter code of the country the story is mainly about, or "" if it is about
  several countries equally or none. Not the country of the newspaper.
- sure: true if the answer is clear from the headlines.
Answer for every number."""
    out = llm(prompt, schema([d.key for d in desks]))
    return [r for r in out.get("stories") or [] if isinstance(r.get("n"), int) and 1 <= r["n"] <= len(stories)]


def run_review(conn: psycopg.Connection, llm=None, max_seconds: float = 120) -> int:
    if os.environ.get("DESK_REVIEW", "on") == "off":
        return 0
    stories = conn.execute(PENDING, (PER_RUN,)).fetchall()
    conn.commit()
    if not stories:
        return 0
    desks = load_desks()
    keys = {d.key for d in desks}
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    heads: dict[int, list[str]] = {}
    for r in conn.execute(
            """SELECT story_id, coalesce(title_en, title) AS t FROM (
                 SELECT story_id, title_en, title, row_number() OVER (PARTITION BY story_id ORDER BY published_at) rn
                 FROM items WHERE story_id = ANY(%s)) x WHERE rn <= 4""", ([s["id"] for s in stories],)):
        heads.setdefault(r["story_id"], []).append(r["t"])
    conn.commit()
    started, done, moved, cold, rescued = time.time(), 0, 0, 0, 0
    relocate: list[tuple[int, str]] = []
    for i in range(0, len(stories), BATCH):
        if time.time() - started > max_seconds:
            break
        batch = stories[i:i + BATCH]
        try:
            answers = review_batch(llm, desks, batch, heads)
        except Exception as e:
            log.warning("desk review failed: %s", e)
            break
        writes = []
        for a in answers:
            s = batch[a["n"] - 1]
            desk = a["desk"] if a["desk"] in keys else None
            country = (a.get("country") or "").strip().upper()[:2]
            review = {"desk": desk, "was": s["desk"], "country": country if len(country) == 2 else "", "sure": bool(a.get("sure")),
                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            # Only a confident answer moves a story; an unsure one is recorded and left alone.
            if not s["routed"]:
                if review["sure"] and desk:
                    rescued += 1
                    writes.append((desk, True, None, Jsonb(review), s["item_count"], s["id"]))
                else:
                    writes.append((s["desk"], False, "no_desk_match", Jsonb(review), s["item_count"], s["id"]))
            elif review["sure"] and desk != s["desk"]:
                if desk:
                    moved += 1
                else:
                    cold += 1
                writes.append((desk or s["desk"], desk is not None, None if desk else "desk_review_none", Jsonb(review), s["item_count"], s["id"]))
            else:
                writes.append((s["desk"], True, None, Jsonb(review), s["item_count"], s["id"]))
            if review["sure"] and review["country"] and _weak(s) and s["place_country"] and s["place_country"] != review["country"]:
                relocate.append((s["id"], review["country"]))
        writes.sort(key=lambda w: w[-1])
        lock_stories(conn, [w[-1] for w in writes])
        with conn.cursor() as cur:
            cur.executemany(
                """UPDATE stories SET desk = %s, routed = %s, excluded_reason = %s, desk_review = %s, desk_reviewed_items = %s,
                          updated_at = now() WHERE id = %s""", writes)
        conn.commit()
        done += len(writes)
    moved_dots = sum(_move_dot(conn, sid, cc) for sid, cc in relocate)
    conn.commit()
    if done:
        log.info("desk review: %d stories checked, %d moved to another desk, %d to cold storage, %d brought back from cold "
                 "storage, %d dots moved to the right country", done, moved, cold, rescued, moved_dots)
    return done


def _weak(s: dict) -> bool:
    """A location resting on weak evidence: only GDELT's tagger, or little of the story's evidence."""
    return not s["location_locked"] and (s["location_source"] == "tagger" or (s["location_confidence"] or 0) < 0.4)


def _move_dot(conn, story_id: int, country: str) -> int:
    """Put the story on its strongest place in the right country, or on the country itself."""
    from ..collectors.base import _place_id
    from ..geo import gazetteer
    from ..locate import set_location

    best = conn.execute(
        """SELECT p.id FROM story_places sp JOIN places p ON p.id = sp.place_id
           WHERE sp.story_id = %s AND p.country = %s AND sp.weight > 0 ORDER BY sp.weight DESC, (p.kind = 'country') LIMIT 1""",
        (story_id, country)).fetchone()
    place_id = best["id"] if best else None
    if place_id is None:
        c = gazetteer().country(country)
        if c is None:
            return 0
        place_id = _place_id(conn, c, {})
    set_location(conn, story_id, place_id, "review", 0.6)
    return 1
