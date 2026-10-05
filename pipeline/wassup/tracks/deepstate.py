"""The Ukraine front line from DeepStateMap (deepstatemap.live).

DeepState, a Ukrainian OSINT group, publishes its map of the war several times a day and keeps
every version since April 2022. Wassup keeps the last version of each day: the areas Russia
holds, the areas that are contested, the areas Ukraine took back, and the arrows marking
directions of attack. History fills in from the newest day backwards, 100 days per hourly run,
so the full history (about 1,500 days, some 250 MB) arrives over the first day without
hammering their server.

Not kept: their markers for Russian units and airfields, and the satirical "territories" they
draw (East Prussia, Karelia and so on).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..collectors.base import Collector
from ..config import settings

log = logging.getLogger(__name__)

API = "https://deepstatemap.live/api/history"
TRACK = {"key": "ukraine-front", "name": "Ukraine front line", "kind": "front", "desk": "russia_ukraine",
         "source": "DeepStateMap", "description": "Areas held by Russia, contested and taken back, from DeepStateMap's daily map."}

# The tag at the end of each feature's name says what it is.
CATEGORY = {
    "status.occupied": "occupied", "territories.crimea": "occupied", "territories.ordlo": "occupied",
    "territories.tuzla": "occupied", "status.unknown": "contested",
    "status.dismissed": "liberated", "status.dismissed_at": "liberated",
}
AREAS = ("occupied", "contested", "liberated")
SIMPLIFY_DEG = 0.0008  # about 60 to 90 m, invisible at the zooms a front is viewed at
MIN_HOLE_M2 = 500_000  # holes smaller than half a square km are slivers between hand drawn areas
_TAG = re.compile(r"geoJSON\.([\w.]+)")
_ARROW = re.compile(r"\{icon=arrow_(\d+)")
KYIV = timezone(timedelta(hours=3))


# Maps from before the tags were added (2022) only have names, in Ukrainian and later also
# English, and DeepState's usual fill colours.
_NAMED = [
    ("skip", re.compile(r"придністров|transnistria|абхаз|abkhaz|цхінвал|tskhinval|пруссі|prussia|карелі|karelia|ічкері|ichkeri"
                       r"|естоні|estoni|латві|latvi|петсамо|petsamo|салла|salla|печор|pechor|курил|kuril", re.I)),
    ("occupied", re.compile(r"окупован|occupied|зайнят\w* ворогом|ордло|ordlo|cadr|крим|crimea|тузла|tuzla", re.I)),
    ("liberated", re.compile(r"звільнен|liberated", re.I)),
    ("contested", re.compile(r"під питанням|невідомо|статус невідомий|unknown", re.I)),
]
_FILL = {"#a52714": "occupied", "#0288d1": "liberated", "#0f9d58": "liberated", "#bcaaa4": "contested"}
_ATTACK = re.compile(r"напрямок удару|direction of attack", re.I)


def classify(feature: dict) -> tuple[str | None, dict]:
    p = feature.get("properties") or {}
    name = p.get("name") or ""
    m = _TAG.search(name)
    tag = m.group(1) if m else ""
    gtype = (feature.get("geometry") or {}).get("type")
    if gtype == "Point":
        if tag == "status.attack_direction" or (not tag and _ATTACK.search(name)):
            a = _ARROW.search(p.get("description") or "")
            # The arrow icons are numbered in steps of 22.5 degrees clockwise from north.
            return "attack", {"bearing": (int(a.group(1)) * 22.5) % 360 if a else None}
        return None, {}
    if gtype not in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return None, {}
    if tag:
        return CATEGORY.get(tag), {}
    for cat, rx in _NAMED:
        if rx.search(name):
            return (None if cat == "skip" else cat), {}
    return _FILL.get(str(p.get("fill") or "").lower()), {}


def daily_snapshots(history: list[dict]) -> list[dict]:
    """The last update of each day (Kyiv time), newest first."""
    best: dict[str, dict] = {}
    for h in history:
        t = datetime.fromisoformat(h["createdAt"].replace("Z", "+00:00"))
        day = t.astimezone(KYIV).date().isoformat()
        if day not in best or t > best[day]["t"]:
            best[day] = {"id": str(h["id"]), "t": t, "day": day}
    return sorted(best.values(), key=lambda x: x["t"], reverse=True)


def ensure_track(conn: psycopg.Connection) -> int:
    return conn.execute(
        """INSERT INTO tracks (key, name, kind, desk, source, description) VALUES (%(key)s, %(name)s, %(kind)s, %(desk)s, %(source)s, %(description)s)
           ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description RETURNING id""", TRACK).fetchone()["id"]


def store_snapshot(conn: psycopg.Connection, track_id: int, ref: str, observed_at: datetime, geojson: dict) -> int:
    areas: dict[str, list[str]] = {c: [] for c in AREAS}
    attacks = []
    for f in geojson.get("features") or []:
        cat, props = classify(f)
        if cat in areas:
            areas[cat].append(json.dumps(f["geometry"]))
        elif cat == "attack":
            attacks.append((json.dumps(f["geometry"]), props))
    snap = conn.execute(
        "INSERT INTO track_snapshots (track_id, observed_at, source_ref) VALUES (%s, %s, %s) RETURNING id",
        (track_id, observed_at, ref)).fetchone()["id"]
    stats = {}
    for cat, geoms in areas.items():
        if not geoms:
            continue
        # Union the pieces, repair them, and simplify a little: thousands of vertices per day
        # add up over four years.
        row = conn.execute(
            """WITH g AS (
                 SELECT ST_CollectionExtract(ST_MakeValid(ST_UnaryUnion(ST_Collect(ST_MakeValid(ST_CollectionExtract(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(x), 4326)), 3))))), 3) u
                 FROM unnest(%s::text[]) x),
               s AS (SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(wassup_fill_small_holes(
                       ST_CollectionExtract(ST_MakeValid(ST_SimplifyPreserveTopology(u, %s)), 3), %s)), 3)) geom FROM g)
               INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom)
               SELECT %s, %s, %s, %s, geom FROM s WHERE NOT ST_IsEmpty(geom)
               RETURNING ST_Area(geom::geography) / 1e6 AS km2""",
            (geoms, SIMPLIFY_DEG, MIN_HOLE_M2, track_id, snap, observed_at, cat)).fetchone()
        if row:
            stats[f"{cat}_km2"] = round(row["km2"], 1)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom, props)
               VALUES (%s, %s, %s, 'attack', ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), %s)""",
            [(track_id, snap, observed_at, g, Jsonb(p)) for g, p in attacks])
    stats["attacks"] = len(attacks)
    conn.execute("UPDATE track_snapshots SET stats = %s WHERE id = %s", (Jsonb(stats), snap))
    return snap


class DeepStateCollector(Collector):
    key = "deepstate"
    interval_s = 3600

    def __init__(self, per_run: int = 100, client: httpx.Client | None = None):
        self.per_run = per_run
        self.client = client or httpx.Client(timeout=60, headers={"User-Agent": settings().http_user_agent})

    def run(self, conn: psycopg.Connection) -> int:
        track = ensure_track(conn)
        conn.commit()
        r = self.client.get(f"{API}/public")
        r.raise_for_status()
        have = {x["source_ref"] for x in conn.execute("SELECT source_ref FROM track_snapshots WHERE track_id = %s", (track,))}
        todo = [s for s in daily_snapshots(r.json()) if s["id"] not in have]
        # When a day's map is updated again later that day, the newer version replaces it.
        done = 0
        for s in todo[: self.per_run]:
            g = self.client.get(f"{API}/{s['id']}/geojson")
            if g.status_code != 200:
                log.warning("deepstate snapshot %s: HTTP %s", s["id"], g.status_code)
                continue
            conn.execute("""DELETE FROM track_snapshots WHERE track_id = %s
                            AND ((observed_at AT TIME ZONE 'UTC') + interval '3 hours')::date = %s::date""", (track, s["day"]))
            store_snapshot(conn, track, s["id"], s["t"], g.json())
            conn.commit()
            done += 1
            time.sleep(0.5)
        if done:
            log.info("deepstate: stored %d daily maps (%d older days still to fetch)", done, max(0, len(todo) - done))
        return done
