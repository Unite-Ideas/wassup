"""Search for the bar at the top: stories from the last 30 days, cold storage included.

- Every word, in any order, in any article's headline or summary (original or English), with
  accents and case ignored: "marche enfants rouges" finds "Marché des Enfants-Rouges".
- If that finds little, close spellings count too ("entants" finds "enfants").
- Stories close in meaning, from the same multilingual embeddings that group articles into
  stories, so an English query finds French coverage and the other way round.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from . import db

log = logging.getLogger(__name__)

# The same expression the index is built on (db/schema.sql items_search_idx); must match exactly.
TEXT = "wassup_norm(coalesce(i.title_en, '') || ' ' || coalesce(i.title, '') || ' ' || coalesce(i.summary, ''))"
STOP = {"the", "and", "for", "with", "des", "les", "une", "der", "die", "das", "und", "del", "los", "las", "con", "por", "dans", "sur",
        "aux", "pour", "par", "est", "of", "in", "on", "at", "to", "a", "an", "de", "la", "le", "du", "el", "en", "et", "y", "e", "i"}
STORY = """s.id, coalesce(s.title_en, s.title) AS title, s.title AS title_original, s.title_tier, s.desk, s.routed, s.excluded_reason,
    s.significance, s.relevance, s.breaking, s.velocity, s.lat, s.lon, s.item_count, s.source_count, s.country_count,
    s.language_count, s.first_seen, s.last_seen, s.primary_place_id, s.location_confidence, s.location_source, s.location_locked"""
FUZZY = 0.4   # how close a misspelled word must be (pg_trgm word similarity, 0 to 1; one wrong letter in 7 is 0.45)
FUZZY_ALONE = 0.55
MEANING = 0.42  # how close in meaning (cosine distance with bge-m3): related stories measured 0.16 to 0.37, unrelated from 0.48


def words(q: str) -> list[str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", q.lower()) if not unicodedata.combining(c))
    ws = [w for w in re.split(r"[^\w]+", plain) if w]
    kept = [w for w in ws if w not in STOP and len(w) >= 2]
    return (kept or ws)[:8]


def _by_words(conn, ws: list[str], fuzzy: bool, limit: int) -> list[dict]:
    conds, params = [], {}
    for n, w in enumerate(ws):
        params[f"l{n}"] = f"%{w.replace('%', '').replace('_', '')}%"
        params[f"w{n}"] = w
        conds.append(f"({TEXT} LIKE %(l{n})s" + (f" OR %(w{n})s <%% {TEXT}" if fuzzy and len(w) >= 4 else "") + ")")
    if fuzzy:
        # One word alone has nothing to keep a loose match in check, so it must be closer.
        conn.execute(f"SET LOCAL pg_trgm.word_similarity_threshold = {FUZZY if len(ws) > 1 else FUZZY_ALONE}")
        if len(ws) > 1:
            # A misspelled word may be close, but at least one word must be spelled as written,
            # so loose matches cannot pile up into nonsense.
            conds.append("(" + " OR ".join(f"{TEXT} LIKE %(l{n})s" for n in range(len(ws))) + ")")
    # Closest spellings first: how well every word matches, added up.
    score = " + ".join(f"word_similarity(%(w{n})s, {TEXT})" for n in range(len(ws))) if fuzzy else "0"
    rows = conn.execute(
        f"""WITH hits AS (
              SELECT i.story_id, count(*) AS n, max({score}) AS closeness FROM items i
              WHERE i.published_at > now() - interval '30 days' AND i.story_id IS NOT NULL AND {' AND '.join(conds)}
              GROUP BY i.story_id)
            SELECT {STORY}, h.n AS matched FROM hits h JOIN stories s ON s.id = h.story_id
            ORDER BY {"h.closeness DESC, " if fuzzy else ""}s.routed DESC, h.n DESC, s.significance DESC, s.last_seen DESC LIMIT %(limit)s""",
        {**params, "limit": limit}).fetchall()
    return rows


def _by_meaning(conn, q: str, skip: set[int], limit: int) -> list[dict]:
    from .embed import get_embedder

    try:
        v = get_embedder().embed([q])[0]
    except Exception as e:  # noqa: BLE001
        log.info("search: no embeddings for meaning (%s)", e)
        return []
    rows = conn.execute(
        f"""SELECT {STORY}, (s.centroid <=> %(v)s) AS distance FROM stories s
            WHERE s.centroid IS NOT NULL AND s.last_seen > now() - interval '30 days'
            ORDER BY s.centroid <=> %(v)s LIMIT %(n)s""", {"v": v, "n": limit + len(skip)}).fetchall()
    return [r for r in rows if r["id"] not in skip and r["distance"] <= MEANING][:limit]


def search(q: str, limit: int = 25, meaning: int = 8) -> dict:
    ws = words(q)
    if not ws:
        return {"words": [], "matches": [], "fuzzy": False, "related": []}
    with db.connect() as conn:
        matches = _by_words(conn, ws, False, limit)
        fuzzy = False
        if len(matches) < 3:
            more = _by_words(conn, ws, True, limit)
            seen = {m["id"] for m in matches}
            extra = [m for m in more if m["id"] not in seen]
            if extra:
                fuzzy = True
                matches = matches + [{**m, "close_spelling": True} for m in extra][: limit - len(matches)]
        conn.commit()
        related = _by_meaning(conn, q, {m["id"] for m in matches}, meaning) if meaning else []
    return {"words": ws, "matches": matches, "fuzzy": fuzzy, "related": related}
