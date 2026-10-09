"""What a DeepStateMap attack arrow means, worked out by Wassup.

DeepState draws an arrow where its analysts see an active push, pointing the way it goes, and
gives nothing else: no name, no date range. For one arrow this adds:

- where: the nearest town, and the town it points towards (the largest place ahead of it, within
  about 40 km and 35 degrees either side of its heading);
- how long: on how many days DeepState has marked an attack within 10 km, and since when without
  a break of more than a few days;
- whether it is working: ground that changed hands within 15 km over the last week and month;
- what else is happening there: strikes reported within 25 km in the last two weeks, and news
  stories placed within 30 km in the last week.
"""
from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import psycopg

from .strikes import places

NEAR_KM = 10       # arrows this close count as the same push
GROUND_KM = 15     # radius for ground taken and retaken
STRIKE_KM = 25
STORY_KM = 30
AHEAD_KM = 40
AHEAD_DEG = 35
GAP_DAYS = 4       # a push is "continuous" across gaps no longer than this

_grid = None


def _place_arrays():
    global _grid
    if _grid is None:
        uniq = {p[0]: p for lst in places().by_name.values() for p in lst}
        rows = list(uniq.values())
        _grid = (rows, np.radians(np.array([[p[5], p[6]] for p in rows])) if rows else np.zeros((0, 2)))
    return _grid


def _km_bearing(lat: float, lon: float, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    la, lo = math.radians(lat), math.radians(lon)
    plat, plon = pts[:, 0], pts[:, 1]
    dlon = plon - lo
    a = np.sin((plat - la) / 2) ** 2 + math.cos(la) * np.cos(plat) * np.sin(dlon / 2) ** 2
    dist = 2 * 6371 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    y = np.sin(dlon) * np.cos(plat)
    x = math.cos(la) * np.sin(plat) - math.sin(la) * np.cos(plat) * np.cos(dlon)
    return dist, (np.degrees(np.arctan2(y, x)) + 360) % 360


def where(lat: float, lon: float, bearing: float | None) -> dict:
    """The nearest town, and the town the arrow points towards."""
    rows, pts = _place_arrays()
    if not rows:
        return {"near": None, "towards": None}
    dist, brg = _km_bearing(lat, lon, pts)
    pop = np.array([p[4] for p in rows])

    def describe(i):
        p = rows[i]
        return {"name": p[1], "admin1": p[3], "country": p[2], "km": round(float(dist[i]), 1), "population": int(p[4])}

    close = np.where((dist <= 15) & (pop >= 200))[0]
    near = describe(int(close[np.argmin(dist[close])])) if len(close) else describe(int(np.argmin(dist)))
    towards = None
    if bearing is not None:
        off = np.abs(((brg - bearing) + 180) % 360 - 180)
        ahead = np.where((dist >= 2) & (dist <= AHEAD_KM) & (off <= AHEAD_DEG))[0]
        if len(ahead):
            # Bigger and closer to the line of the arrow wins: the place the push is aimed at.
            score = np.log10(pop[ahead] + 10) - off[ahead] / 60 - dist[ahead] / 80
            towards = describe(int(ahead[np.argmax(score)]))
    return {"near": near, "towards": towards}


def persistence(conn: psycopg.Connection, track_id: int, lat: float, lon: float, day) -> dict:
    """How long DeepState has marked this push. Walks back through the maps Wassup has (daily
    lately, sparser further back), following the arrow as it moves with the front: each day's
    arrow must be within NEAR_KM of the one found the day after. The run ends at the first
    stretch of more than GAP_DAYS of maps without one. A gap in Wassup's own history ends it too,
    marked as "at least", since the push may be older."""
    maps = [r["d"] for r in conn.execute(
        "SELECT DISTINCT observed_at::date AS d FROM track_snapshots WHERE track_id = %s AND observed_at::date <= %s ORDER BY d DESC",
        (track_id, day))]
    arrows: dict = {}
    for r in conn.execute(
            """SELECT observed_at::date AS d, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM track_observations
               WHERE track_id = %s AND category = 'attack' AND observed_at::date <= %s
                 AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 150000)""",
            (track_id, day, lon, lat)):
        arrows.setdefault(r["d"], []).append((r["lat"], r["lon"]))
    if not maps:
        return {"since": None, "run_days": 0, "at_least": False, "maps_last_year": 0, "marked_last_year": 0}

    def near(d, anchor):
        pts = arrows.get(d) or []
        best = min(pts, key=lambda q: km(anchor, q), default=None)
        return best if best is not None and km(anchor, best) <= NEAR_KM else None

    anchor, since, prev, at_least, marked = (lat, lon), None, None, False, set()
    for d in maps:
        hit = near(d, anchor)
        if prev is not None and since is not None and (prev - d).days > GAP_DAYS and hit is None:
            at_least = prev == since  # Wassup has no maps for a while before this run
            break
        if hit is not None:
            since, anchor = d, hit
            marked.add(d)
        elif since is not None and (since - d).days > GAP_DAYS:
            break
        prev = d
    else:
        at_least = since is not None and since == maps[-1]  # as old as the history Wassup has
    # Over the last year: maps with an arrow near this spot (not followed, so a different push here counts too).
    year = [d for d in maps if (day - d).days < 365]
    here = sum(1 for d in year if near(d, (lat, lon)) is not None or d in marked)
    return {"since": since.isoformat() if since else None, "run_days": (day - since).days + 1 if since else 0,
            "at_least": at_least, "maps_last_year": len(year), "marked_last_year": here}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(min(1.0, h)))


def ground(conn: psycopg.Connection, track_id: int, snap_id: int, observed_at, lat: float, lon: float) -> dict:
    out = {}
    for label, days in (("week", 7), ("month", 30)):
        prev = conn.execute(
            """SELECT id, observed_at FROM track_snapshots WHERE track_id = %s AND observed_at <= %s
               ORDER BY observed_at DESC LIMIT 1""", (track_id, observed_at - timedelta(days=days) + timedelta(hours=6))).fetchone()
        if not prev or prev["id"] == snap_id:
            out[label] = None
            continue
        r = conn.execute(
            """WITH buf AS (SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(m)s)::geometry AS g),
                    a AS (SELECT coalesce(ST_Union(ST_Intersection(o.geom, buf.g)), ST_GeomFromText('POLYGON EMPTY', 4326)) AS g
                          FROM track_observations o, buf WHERE o.snapshot_id = %(now)s AND o.category = 'occupied' AND ST_Intersects(o.geom, buf.g)),
                    b AS (SELECT coalesce(ST_Union(ST_Intersection(o.geom, buf.g)), ST_GeomFromText('POLYGON EMPTY', 4326)) AS g
                          FROM track_observations o, buf WHERE o.snapshot_id = %(prev)s AND o.category = 'occupied' AND ST_Intersects(o.geom, buf.g))
               SELECT ST_Area(ST_Difference(a.g, b.g)::geography) / 1e6 AS taken, ST_Area(ST_Difference(b.g, a.g)::geography) / 1e6 AS retaken
               FROM a, b""", {"lon": lon, "lat": lat, "m": GROUND_KM * 1000, "now": snap_id, "prev": prev["id"]}).fetchone()
        out[label] = {"since": prev["observed_at"].date().isoformat(), "taken_km2": round(r["taken"] or 0, 2),
                      "retaken_km2": round(r["retaken"] or 0, 2)}
    return out


def strikes_near(conn, lat: float, lon: float, observed_at) -> dict:
    rows = conn.execute(
        """SELECT o.observed_at, o.label, o.props->>'weapon' AS weapon, o.props->>'outcome' AS outcome, o.source_url
           FROM track_observations o JOIN tracks t ON t.id = o.track_id AND t.kind = 'strikes'
           WHERE o.category = 'strike' AND o.status <> 'rejected'
             AND o.observed_at BETWEEN %s AND %s
             AND ST_DWithin(o.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
           ORDER BY o.observed_at DESC""", (observed_at - timedelta(days=14), observed_at + timedelta(days=1), lon, lat, STRIKE_KM * 1000)).fetchall()
    return {"count": len(rows), "latest": [{**r, "observed_at": r["observed_at"].isoformat()} for r in rows[:5]]}


def stories_near(conn, lat: float, lon: float, observed_at, prefer_desk: str | None = None) -> list[dict]:
    """Stories about this spot: on a desk (not cold storage), with their dot nearby or a nearby town
    among the places they are mainly about. A passing mention is not enough ("Mayor of Kingstown"
    is a TV show, not news from Kingstown). Stories on `prefer_desk` come first."""
    from ..api import ABOUT, MIN_PLACE_WEIGHT

    rows = conn.execute(
        f"""WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS g),
                hits AS (
                  SELECT s.id FROM stories s, pt WHERE s.lat IS NOT NULL AND s.routed
                    AND s.last_seen BETWEEN %(lo)s AND %(hi)s
                    AND ST_DWithin(ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326)::geography, pt.g, %(m)s)
                  UNION
                  SELECT s.id FROM story_places sp JOIN places p ON p.id = sp.place_id AND p.kind = 'city'
                    JOIN stories s ON s.id = sp.story_id LEFT JOIN places pp ON pp.id = s.primary_place_id, pt
                    WHERE s.routed AND s.last_seen BETWEEN %(lo)s AND %(hi)s AND sp.weight > {MIN_PLACE_WEIGHT}
                      AND ST_DWithin(p.geom, pt.g, %(m)s) AND {ABOUT})
           SELECT s.id, coalesce(s.title_en, s.title) AS title, s.last_seen, s.item_count, s.significance
           FROM stories s JOIN hits h ON h.id = s.id
           ORDER BY s.desk IS NOT DISTINCT FROM %(desk)s DESC, s.significance DESC, s.last_seen DESC LIMIT 6""",
        {"lon": lon, "lat": lat, "m": STORY_KM * 1000, "lo": observed_at - timedelta(days=7), "hi": observed_at + timedelta(days=1),
         "desk": prefer_desk}).fetchall()
    return [{**r, "last_seen": r["last_seen"].isoformat()} for r in rows]


def attack_info(conn: psycopg.Connection, obs_id: int) -> dict | None:
    o = conn.execute(
        """SELECT o.id, o.track_id, o.snapshot_id, o.observed_at, o.props, ST_Y(ST_PointOnSurface(o.geom)) AS lat,
                  ST_X(ST_PointOnSurface(o.geom)) AS lon
           FROM track_observations o WHERE o.id = %s AND o.category = 'attack'""", (obs_id,)).fetchone()
    if not o:
        return None
    bearing = (o["props"] or {}).get("bearing")
    return {
        "id": o["id"], "observed_at": o["observed_at"].isoformat(), "lat": o["lat"], "lon": o["lon"], "bearing": bearing,
        "heading": _compass(bearing),
        **where(o["lat"], o["lon"], bearing),
        "persistence": persistence(conn, o["track_id"], o["lat"], o["lon"], o["observed_at"].date()),
        "ground": ground(conn, o["track_id"], o["snapshot_id"], o["observed_at"], o["lat"], o["lon"]),
        "strikes": strikes_near(conn, o["lat"], o["lon"], o["observed_at"]),
        "stories": stories_near(conn, o["lat"], o["lon"], o["observed_at"]),
    }


def _compass(b: float | None) -> str | None:
    if b is None:
        return None
    return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][int(((b % 360) + 22.5) // 45) % 8]
