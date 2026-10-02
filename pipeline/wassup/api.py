"""HTTP API for the Wassup UI. Also serves the built UI from ui/dist when it exists."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import load_yaml, settings
from .newsroom.api import router as newsroom_router

app = FastAPI(title="Wassup", version="0.1.0")
app.include_router(newsroom_router)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])

STORY_COLS = """s.id, coalesce(s.title_en, s.title) AS title, s.title AS title_original, s.title_tier, s.desk, s.routed, s.excluded_reason, s.significance, s.relevance,
    s.breaking, s.velocity, s.lat, s.lon, s.item_count, s.source_count, s.country_count, s.language_count,
    s.first_seen, s.last_seen"""


def _window(since: str | None, until: str | None, hours: float | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(until) if until else now
    if since:
        start = datetime.fromisoformat(since)
    else:
        start = end - timedelta(hours=hours or 24)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _filter(start: datetime, end: datetime, desks: str | None, cold: bool, min_sig: float,
            alias: str = "s") -> tuple[str, dict[str, Any]]:
    """WHERE clause for stories active in the window."""
    where = [f"{alias}.last_seen >= %(start)s", f"{alias}.first_seen <= %(end)s", f"{alias}.significance >= %(min_sig)s"]
    params: dict[str, Any] = {"start": start, "end": end, "min_sig": min_sig}
    if not cold:
        where.append(f"{alias}.routed")
    if desks:
        params["desks"] = [d for d in desks.split(",") if d]
        where.append(f"({alias}.desk = ANY(%(desks)s)" + (f" OR {alias}.desk IS NULL)" if "none" in params["desks"] else ")"))
    return " AND ".join(where), params


class Window:
    """Common query parameters for anything filtered by time and desk."""

    def __init__(self, since: str | None = None, until: str | None = None, hours: float | None = None,
                 desks: str | None = None, cold: bool = False, min_sig: float = 0.0):
        self.start, self.end = _window(since, until, hours)
        self.where, self.params = _filter(self.start, self.end, desks, cold, min_sig)


@app.get("/api/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/desks")
def desks() -> list[dict]:
    return [{"key": d["key"], "name": d["name"], "color": d.get("color", "#888"), "description": d.get("description", "")}
            for d in load_yaml("desks.yaml").get("desks", [])]


@app.get("/api/stats")
def stats() -> dict:
    with db.connect() as conn:
        r = conn.execute(
            """SELECT (SELECT count(*) FROM items) items,
                      (SELECT count(*) FROM items WHERE collected_at > now() - interval '1 hour') items_last_hour,
                      (SELECT count(*) FROM stories) stories,
                      (SELECT count(*) FROM stories WHERE routed) routed,
                      (SELECT count(*) FROM stories WHERE NOT routed) cold,
                      (SELECT count(*) FROM stories WHERE breaking) breaking,
                      (SELECT count(*) FROM story_links) links,
                      (SELECT count(*) FROM sources) sources,
                      (SELECT count(*) FROM sources WHERE last_error IS NOT NULL) sources_failing,
                      (SELECT count(DISTINCT language) FROM items) languages,
                      (SELECT min(published_at) FROM items) oldest,
                      (SELECT max(published_at) FROM items) newest""").fetchone()
    s = settings()
    return {**r, "embed_backend": s.embed_backend, "triage_backend": s.triage_backend}


@app.get("/api/sources")
def sources() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT key, name, kind, url, country, language, trust_tier, state_media, last_polled_at, last_error, item_count
               FROM sources ORDER BY kind, name""").fetchall()


@app.get("/api/globe")
def globe(since: str | None = None, until: str | None = None, hours: float | None = None, desks: str | None = None,
          cold: bool = False, min_sig: float = 0.0, limit: int = Query(1500, le=5000)) -> dict:
    """Everything the globe draws for a time window: story points, place nodes, and links."""
    w = Window(since, until, hours, desks, cold, min_sig)
    with db.connect() as conn:
        stories = conn.execute(
            f"SELECT {STORY_COLS} FROM stories s WHERE {w.where} AND s.lat IS NOT NULL "
            "ORDER BY s.breaking DESC, s.significance DESC, s.item_count DESC LIMIT %(limit)s",
            {**w.params, "limit": limit}).fetchall()
        places = conn.execute(
            f"""SELECT p.id, p.name, p.country, p.kind, p.lat, p.lon,
                       count(DISTINCT s.id) story_count, sum(sp.weight) item_count,
                       max(s.significance) max_significance, bool_or(s.breaking) breaking,
                       mode() WITHIN GROUP (ORDER BY s.desk) top_desk
                FROM story_places sp JOIN stories s ON s.id = sp.story_id JOIN places p ON p.id = sp.place_id
                WHERE {w.where} GROUP BY p.id ORDER BY story_count DESC LIMIT 2000""", w.params).fetchall()
        ids = [s["id"] for s in stories]
        links = conn.execute(
            """SELECT a, b, kind, weight, evidence, created_by FROM story_links
               WHERE a = ANY(%(ids)s) AND b = ANY(%(ids)s)
               ORDER BY created_by LIKE 'agent:%%' DESC, weight DESC LIMIT 4000""", {"ids": ids}).fetchall() if ids else []
    return {"window": {"start": w.start, "end": w.end}, "stories": stories, "places": places, "links": links}


@app.get("/api/stories")
def list_stories(since: str | None = None, until: str | None = None, hours: float | None = None, desks: str | None = None,
                 cold: bool = False, min_sig: float = 0.0, q: str | None = None, breaking: bool = False,
                 limit: int = Query(100, le=1000)) -> list[dict]:
    w = Window(since, until, hours, desks, cold, min_sig)
    where, params = w.where, dict(w.params)
    if q:
        where += " AND (s.title ILIKE %(q)s OR s.title_en ILIKE %(q)s)"
        params["q"] = f"%{q}%"
    if breaking:
        where += " AND s.breaking"
    with db.connect() as conn:
        return conn.execute(
            f"SELECT {STORY_COLS} FROM stories s WHERE {where} ORDER BY s.breaking DESC, s.significance DESC, s.last_seen DESC LIMIT %(limit)s",
            {**params, "limit": limit}).fetchall()


def _neighbors(conn, story_id: int) -> list[dict]:
    return conn.execute(
        f"""SELECT l.kind, l.weight, l.evidence, l.created_by, {STORY_COLS}
            FROM story_links l JOIN stories s ON s.id = CASE WHEN l.a = %(id)s THEN l.b ELSE l.a END
            WHERE l.a = %(id)s OR l.b = %(id)s ORDER BY l.weight DESC LIMIT 50""", {"id": story_id}).fetchall()


@app.get("/api/stories/{story_id}")
def story(story_id: int) -> dict:
    with db.connect() as conn:
        s = conn.execute(f"SELECT {STORY_COLS}, s.triage FROM stories s WHERE s.id = %s", (story_id,)).fetchone()
        if not s:
            raise HTTPException(404, "story not found")
        items = conn.execute(
            """SELECT i.id, coalesce(i.title_en, i.title) AS title, CASE WHEN i.title_en IS NOT NULL THEN i.title END AS title_original,
                      i.summary, i.url, i.language, i.published_at, i.meta,
                      coalesce(i.outlet, src.name) outlet, coalesce(i.outlet_tier, src.trust_tier) tier,
                      (i.outlet_state OR src.state_media) state_media, src.kind source_kind
               FROM items i JOIN sources src ON src.id = i.source_id
               WHERE i.story_id = %s ORDER BY i.published_at DESC LIMIT 300""", (story_id,)).fetchall()
        places = conn.execute(
            """SELECT p.id, p.name, p.country, p.kind, p.lat, p.lon, sp.weight FROM story_places sp
               JOIN places p ON p.id = sp.place_id WHERE sp.story_id = %s
               ORDER BY sp.weight DESC, (p.kind = 'country'), p.id""", (story_id,)).fetchall()
        entities = conn.execute(
            """SELECT e.id, e.kind, e.name, count(*) mentions FROM items i JOIN item_entities ie ON ie.item_id = i.id
               JOIN entities e ON e.id = ie.entity_id WHERE i.story_id = %s
               GROUP BY e.id ORDER BY mentions DESC LIMIT 20""", (story_id,)).fetchall()
        feedback = conn.execute("SELECT value FROM feedback WHERE story_id = %s ORDER BY created_at DESC LIMIT 1", (story_id,)).fetchone()
        briefs = conn.execute(
            """SELECT b.id, b.kind, b.agent_key, a.name AS agent_name, b.body, b.meta, b.created_at FROM briefs b
               LEFT JOIN newsroom_agents a ON a.key = b.agent_key WHERE b.story_id = %s ORDER BY b.created_at DESC LIMIT 10""",
            (story_id,)).fetchall()
        followers = conn.execute(
            """SELECT f.agent_key, a.name AS agent_name, f.reason FROM follows f LEFT JOIN newsroom_agents a ON a.key = f.agent_key
               WHERE f.story_id = %s AND f.active""", (story_id,)).fetchall()
        return {**s, "items": items, "places": places, "entities": entities, "links": _neighbors(conn, story_id),
                "feedback": feedback["value"] if feedback else 0, "briefs": briefs, "followers": followers}


@app.get("/api/places/{place_id}")
def place(place_id: int, since: str | None = None, until: str | None = None, hours: float | None = None,
          desks: str | None = None, cold: bool = False, min_sig: float = 0.0) -> dict:
    w = Window(since, until, hours, desks, cold, min_sig)
    with db.connect() as conn:
        p = conn.execute("SELECT id, name, country, kind, lat, lon FROM places WHERE id = %s", (place_id,)).fetchone()
        if not p:
            raise HTTPException(404, "place not found")
        stories = conn.execute(
            f"""SELECT {STORY_COLS}, sp.weight place_weight FROM story_places sp JOIN stories s ON s.id = sp.story_id
                WHERE sp.place_id = %(pid)s AND {w.where}
                ORDER BY s.breaking DESC, s.significance DESC, s.last_seen DESC LIMIT 300""",
            {**w.params, "pid": place_id}).fetchall()
    return {**p, "stories": stories}


@app.get("/api/graph/{story_id}")
def graph(story_id: int, depth: int = Query(2, ge=1, le=3), max_nodes: int = Query(60, le=200)) -> dict:
    """Neighbourhood of a story for the evidence board: linked stories plus the people and
    organizations that connect them."""
    with db.connect() as conn:
        seen = {story_id: 0}
        edges = []
        queue = deque([story_id])
        while queue and len(seen) < max_nodes:
            sid = queue.popleft()
            if seen[sid] >= depth:
                continue
            for n in conn.execute(
                "SELECT a, b, kind, weight, evidence FROM story_links WHERE a = %(id)s OR b = %(id)s ORDER BY weight DESC LIMIT 12",
                {"id": sid}).fetchall():
                other = n["b"] if n["a"] == sid else n["a"]
                if other not in seen:
                    if len(seen) >= max_nodes:
                        continue
                    seen[other] = seen[sid] + 1
                    queue.append(other)
                edges.append({"source": f"s{n['a']}", "target": f"s{n['b']}", "kind": n["kind"], "weight": n["weight"], "evidence": n["evidence"]})
        ids = list(seen)
        stories = conn.execute(f"SELECT {STORY_COLS} FROM stories s WHERE s.id = ANY(%s)", (ids,)).fetchall()
        ents = conn.execute(
            """SELECT * FROM (
                 SELECT i.story_id, e.id, e.kind, e.name, count(*) n,
                        row_number() OVER (PARTITION BY i.story_id ORDER BY count(*) DESC) rn
                 FROM items i JOIN item_entities ie ON ie.item_id = i.id JOIN entities e ON e.id = ie.entity_id
                 WHERE i.story_id = ANY(%s) GROUP BY i.story_id, e.id) x WHERE rn <= 5""", (ids,)).fetchall()
    # Keep entities that tie two or more stories together, plus the root story's top people.
    by_entity: dict[int, list[dict]] = {}
    for e in ents:
        by_entity.setdefault(e["id"], []).append(e)
    nodes = [{**s, "id": f"s{s['id']}", "story_id": s["id"], "type": "story", "depth": seen[s["id"]]} for s in stories]
    present = {n["id"] for n in nodes}
    uniq = {(e["source"], e["target"], e["kind"]): e for e in edges if e["source"] in present and e["target"] in present}
    for eid, rows in by_entity.items():
        if len(rows) >= 2 or any(r["story_id"] == story_id and r["rn"] <= 3 for r in rows):
            nodes.append({"id": f"e{eid}", "type": "entity", "kind": rows[0]["kind"], "name": rows[0]["name"], "stories": len(rows)})
            for r in rows:
                uniq[(f"s{r['story_id']}", f"e{eid}", "mentions")] = {"source": f"s{r['story_id']}", "target": f"e{eid}", "kind": "mentions", "weight": r["n"]}
    return {"root": f"s{story_id}", "nodes": nodes, "edges": list(uniq.values())}


@app.get("/api/timeline")
def timeline(since: str | None = None, until: str | None = None, hours: float | None = 168, desks: str | None = None,
             cold: bool = False, buckets: int = Query(168, ge=10, le=1000)) -> dict:
    """Article counts per time bucket and desk, for the timeline scrubber."""
    start, end = _window(since, until, hours)
    where, params = _filter(start, end, desks, cold, 0.0)
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT width_bucket(extract(epoch FROM i.published_at), %(lo)s, %(hi)s, %(n)s) - 1 bucket,
                       coalesce(s.desk, 'none') desk, count(*) n, count(*) FILTER (WHERE s.breaking) breaking
                FROM items i JOIN stories s ON s.id = i.story_id
                WHERE i.published_at >= %(start)s AND i.published_at <= %(end)s AND {where}
                GROUP BY 1, 2 ORDER BY 1""",
            {**params, "lo": start.timestamp(), "hi": end.timestamp() + 1e-6, "n": buckets}).fetchall()
    return {"start": start, "end": end, "buckets": buckets, "counts": rows}


class FeedbackIn(BaseModel):
    value: int


@app.post("/api/stories/{story_id}/feedback")
def feedback(story_id: int, body: FeedbackIn) -> dict:
    if body.value not in (-1, 1):
        raise HTTPException(400, "value must be 1 or -1")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM stories WHERE id = %s", (story_id,)).fetchone():
            raise HTTPException(404, "story not found")
        conn.execute("INSERT INTO feedback (story_id, value) VALUES (%s, %s)", (story_id, body.value))
        # Queue the story for triage again so the feedback takes effect within seconds.
        conn.execute("UPDATE stories SET triaged_item_count = 0 WHERE id = %s", (story_id,))
        conn.commit()
    return {"ok": True}


if settings().ui_dist.is_dir():
    app.mount("/", StaticFiles(directory=settings().ui_dist, html=True), name="ui")
