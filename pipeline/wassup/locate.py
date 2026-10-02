"""Checks the locations of stories whose placement rests on weak evidence.

The rules in collectors/base.py weigh every place a story mentions. When the winning place is
not named in any headline and has little of the evidence, Wassup asks one multiple choice
question: which of these places is this story mainly about, or none? Jev answers it when
configured (a fraction of a cent, calibrated confidence); otherwise the local model does. The
answer locks the location so new articles do not move it, until the story doubles in size.

Fixes you make in the story panel go through set_location() and are recorded, so a place you
keep removing is distrusted when GDELT's tagger is its only support.
"""
from __future__ import annotations

import json
import logging
import time

import httpx
import psycopg

from .config import settings

log = logging.getLogger(__name__)

NEEDS_CHECK = """
    SELECT s.id, coalesce(s.title_en, s.title) AS title, s.item_count, s.primary_place_id
    FROM stories s
    WHERE s.routed AND NOT s.location_locked AND s.lat IS NOT NULL
      AND s.last_seen > now() - interval '3 days'
      AND (s.location_source = 'tagger' OR s.location_confidence < 0.4)
      AND (s.location_checked_at IS NULL OR s.item_count >= 2 * s.location_checked_items)
    ORDER BY s.significance DESC LIMIT %s"""


def _candidates(conn, story_id: int) -> list[dict]:
    """Places the story mentions, strongest evidence first, plus the countries of any cities
    (often a story is about a country rather than the city that was tagged). Places you
    removed are left out."""
    rows = conn.execute(
        """SELECT p.id, p.name, p.country, p.kind FROM story_places sp JOIN places p ON p.id = sp.place_id
           WHERE sp.story_id = %s AND sp.weight > 0 ORDER BY sp.weight DESC LIMIT 8""", (story_id,)).fetchall()
    from .collectors.base import _place_id
    from .geo import gazetteer

    cache: dict[str, int] = {}
    for c in sorted({r["country"] for r in rows if r["country"] and r["kind"] != "country"}):
        p = gazetteer().country(c)
        if p:
            rows.append({"id": _place_id(conn, p, cache), "name": p.name, "country": p.country, "kind": "country"})
    return list({r["id"]: r for r in rows}.values())[:12]


def _context(conn, story_id: int) -> dict:
    items = conn.execute(
        """SELECT DISTINCT coalesce(title_en, title) t, summary FROM items WHERE story_id = %s
           ORDER BY 1 LIMIT 8""", (story_id,)).fetchall()
    return {"headlines": [i["t"] for i in items], "summaries": [i["summary"][:300] for i in items if i["summary"]][:3]}


def _label(p: dict) -> str:
    return f"{p['name']}" + (f", {p['country']}" if p["country"] and p["kind"] != "country" else "") + f" ({p['kind']})"


class JevLocator:
    name = "jev"

    def __init__(self, client: httpx.Client | None = None):
        from .triage.jev import within_budget

        s = settings()
        self.key, self.model = s.typesafe_api_key, s.jev_model
        self.client = client or httpx.Client(timeout=30)
        self._budget = within_budget

    def available(self) -> bool:
        return bool(self.key) and self._budget()

    def choose(self, ctx: dict, options: dict[str, str]) -> tuple[str, float]:
        from .triage.jev import URL, record_usage

        body = {"model": self.model, "state": ctx, "questions": {"place": {
            "type": "choice",
            "instructions": "Which place is this news story mainly about? Ignore places that only appear in passing, "
                            "such as a street name in a photo caption or an outlet's location.",
            "criteria": options}}}
        r = self.client.post(URL, json=body, headers={"authorization": f"Bearer {self.key}"})
        if r.status_code in (429, 529):
            time.sleep(2)
            r = self.client.post(URL, json=body, headers={"authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        out = r.json()
        u = out.get("usage") or {}
        record_usage(u.get("input_tokens", 0), u.get("output_tokens", 0))
        a = out["answers"]["place"]
        return a["choice"], float(a.get("confidence", 0))


class ModelLocator:
    name = "model"

    def __init__(self, client: httpx.Client | None = None):
        s = settings()
        self.url, self.model = s.ollama_url.rstrip("/"), s.triage_model
        self.client = client or httpx.Client(timeout=120)

    def available(self) -> bool:
        return settings().triage_backend != "rules"

    def choose(self, ctx: dict, options: dict[str, str]) -> tuple[str, float]:
        listing = "\n".join(f"- {k}: {v}" for k, v in options.items())
        prompt = (f"Headlines of one news story:\n{json.dumps(ctx, ensure_ascii=False)}\n\nWhich place is this story mainly about? "
                  f"Ignore places that only appear in passing (a street in a photo caption, an outlet's city). Options:\n{listing}")
        schema = {"type": "object", "properties": {"place": {"type": "string", "enum": list(options)},
                                                   "confidence": {"type": "number"}}, "required": ["place", "confidence"]}
        r = self.client.post(f"{self.url}/api/chat", json={"model": self.model, "stream": False, "think": False, "format": schema,
                                                         "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        a = json.loads(r.json()["message"]["content"])
        return a["place"], float(a.get("confidence", 0.5))


def set_location(conn: psycopg.Connection, story_id: int, place_id: int | None, source: str,
                 confidence: float = 1.0, remove_place_id: int | None = None, record: bool = False) -> None:
    """Pin a story to a place (or take it off the map with place_id None) and lock it."""
    old = conn.execute("SELECT primary_place_id FROM stories WHERE id = %s", (story_id,)).fetchone()
    if remove_place_id:
        # Removing a place zeroes its evidence for this story, so new articles cannot bring it back.
        conn.execute("""UPDATE item_places SET weight = 0 WHERE place_id = %s
                        AND item_id IN (SELECT id FROM items WHERE story_id = %s)""", (remove_place_id, story_id))
        conn.execute("UPDATE story_places SET weight = 0 WHERE story_id = %s AND place_id = %s", (story_id, remove_place_id))
        conn.execute("UPDATE places SET false_positive_count = false_positive_count + 1 WHERE id = %s", (remove_place_id,))
    if place_id:
        p = conn.execute("SELECT lat, lon FROM places WHERE id = %s", (place_id,)).fetchone()
        conn.execute("""INSERT INTO story_places (story_id, place_id, weight) VALUES (%s, %s, 3)
                        ON CONFLICT (story_id, place_id) DO UPDATE SET weight = greatest(story_places.weight, 3)""", (story_id, place_id))
        conn.execute("""UPDATE stories SET primary_place_id = %s, lat = %s, lon = %s, location_source = %s, location_confidence = %s,
                        location_locked = true, location_checked_at = now(), location_checked_items = item_count WHERE id = %s""",
                     (place_id, p["lat"], p["lon"], source, confidence, story_id))
    else:
        conn.execute("""UPDATE stories SET primary_place_id = NULL, lat = NULL, lon = NULL, location_source = %s,
                        location_confidence = %s, location_locked = true, location_checked_at = now(),
                        location_checked_items = item_count WHERE id = %s""", (source, confidence, story_id))
    if record:
        conn.execute("INSERT INTO location_corrections (story_id, from_place_id, to_place_id, by) VALUES (%s, %s, %s, %s)",
                     (story_id, old["primary_place_id"] if old else None, place_id, source))


def unlock(conn: psycopg.Connection, story_id: int) -> None:
    """Let the rules place a story again and queue it for checking (a desk flagged it)."""
    conn.execute("""UPDATE stories SET location_locked = false, location_checked_at = NULL, location_source = 'tagger',
                    location_confidence = 0 WHERE id = %s AND location_source IS DISTINCT FROM 'you'""", (story_id,))


def run_locate(conn: psycopg.Connection, limit: int = 40, max_seconds: float = 60, locators: list | None = None) -> int:
    locators = [l for l in (locators if locators is not None else [JevLocator(), ModelLocator()]) if l.available()]
    if not locators:
        return 0
    started, done = time.time(), 0
    for s in conn.execute(NEEDS_CHECK, (limit,)).fetchall():
        if time.time() - started > max_seconds:
            break
        cands = _candidates(conn, s["id"])
        options = {str(c["id"]): _label(c) for c in cands}
        options["none"] = "No single place: the story is about a topic, a market, or many places, or none of these fit."
        ctx = _context(conn, s["id"])
        choice, conf = None, 0.0
        for loc in locators:
            try:
                choice, conf = loc.choose(ctx, options)
                source = loc.name
                break
            except Exception as e:
                log.warning("%s location check failed for story %s: %s", loc.name, s["id"], e)
        if choice is None:
            continue
        if choice == "none":
            set_location(conn, s["id"], None, source, conf)
        elif choice.isdigit() and int(choice) in {c["id"] for c in cands}:
            set_location(conn, s["id"], int(choice), source, conf)
        conn.commit()
        done += 1
    if done:
        log.info("checked %d story locations", done)
    return done


def relocate(conn: psycopg.Connection, days: int = 7) -> int:
    """Re-score place evidence for recent articles with the current rules (they were stored
    before the rules existed), then re-place their stories. Locations you set stay put."""
    from .cluster import refresh_stories
    from .collectors.base import RawItem, weighted_places
    from .geo import Place

    distrusted = {r["key"] for r in conn.execute("SELECT key FROM places WHERE false_positive_count >= 2")}
    items = conn.execute(
        """SELECT i.id, i.title, i.summary, i.url, i.meta, i.story_id, s.country AS source_country
           FROM items i JOIN sources s ON s.id = i.source_id
           WHERE i.published_at > now() - %s * interval '1 day' AND i.story_id IS NOT NULL""", (days,)).fetchall()
    places = {}
    for r in conn.execute(
        """SELECT ip.item_id, ip.weight, p.id, p.key, p.name, p.country, p.kind, p.lat, p.lon FROM item_places ip
           JOIN places p ON p.id = ip.place_id JOIN items i ON i.id = ip.item_id
           WHERE i.published_at > now() - %s * interval '1 day' ORDER BY ip.item_id, p.id""", (days,)):
        places.setdefault(r["item_id"], []).append(r)
    updates, stories = [], set()
    for it in items:
        rows = [r for r in places.get(it["id"], []) if r["weight"] > 0]  # removed by you: leave at zero
        if not rows:
            continue
        raw = RawItem(url=it["url"], title=it["title"], summary=it["summary"] or "", published_at=None, meta=it["meta"] or {},
                      outlet_country=None if (it["meta"] or {}).get("feed") else it["source_country"],
                      places=[Place(r["key"], r["name"], r["country"], r["kind"], r["lat"], r["lon"]) for r in rows])
        by_key = {r["key"]: r["id"] for r in rows}
        for p, in_title, w in weighted_places(raw, distrusted):
            if p.key in by_key:
                updates.append((w, in_title, it["id"], by_key[p.key]))
        stories.add(it["story_id"])
    with conn.cursor() as cur:
        cur.executemany("UPDATE item_places SET weight = %s, in_title = %s WHERE item_id = %s AND place_id = %s", updates)
    ids = list(stories)
    for i in range(0, len(ids), 500):
        refresh_stories(conn, ids[i:i + 500])
        conn.commit()
    return len(ids)
