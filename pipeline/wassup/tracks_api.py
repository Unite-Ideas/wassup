"""Tracks for the MAP view: what a front or a movement looked like at a given moment."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from . import db
from .tracks.movement import daily_path, km
from .social.media import photos_for
from .tracks.strikes import group_strikes

router = APIRouter(prefix="/api/tracks")

MIN_CHANGE_M2 = 10_000  # ignore changes smaller than a hectare


@router.get("")
def tracks() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT t.id, t.key, t.name, t.kind, t.desk, t.source, t.description, t.story_id,
                      min(o.observed_at) AS first, max(o.observed_at) AS last,
                      (SELECT count(*) FROM track_snapshots s WHERE s.track_id = t.id) AS snapshots,
                      (SELECT stats FROM track_snapshots s WHERE s.track_id = t.id ORDER BY observed_at DESC LIMIT 1) AS latest
               FROM tracks t LEFT JOIN track_observations o ON o.track_id = t.id AND o.status <> 'rejected'
               WHERE t.active GROUP BY t.id ORDER BY t.name""").fetchall()


@router.get("/{track_id}/series")
def series(track_id: int) -> list[dict]:
    """Summary numbers per snapshot (area held, contested, attacks), oldest first."""
    with db.connect() as conn:
        return conn.execute("SELECT observed_at AS t, stats FROM track_snapshots WHERE track_id = %s ORDER BY observed_at",
                            (track_id,)).fetchall()


@router.get("/{track_id}/state")
def state(track_id: int, at: datetime | None = None, compare_days: float = Query(1, ge=0.1, le=365),
          trail_days: float = Query(14, ge=0, le=365)) -> dict:
    """The track as it stood at `at` (default now).

    Fronts: the last snapshot at or before `at`, plus what changed hands since the snapshot
    `compare_days` earlier ("gained" by the occupier, "lost" by it). Strikes: those reported in
    the `compare_days` up to `at`, reports of the same strike grouped into one. Movements: every position
    reported up to `at`, with the day by day path, reports that disagree with it flagged, and a
    summary (latest place, distance covered, last group size).
    """
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    with db.connect() as conn:
        t = conn.execute("SELECT id, kind FROM tracks WHERE id = %s", (track_id,)).fetchone()
        if not t:
            raise HTTPException(404, "track not found")
        if t["kind"] in ("control", "zones"):
            snap = conn.execute(
                "SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s AND observed_at <= %s ORDER BY observed_at DESC LIMIT 1",
                (track_id, at)).fetchone() or conn.execute(
                "SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s ORDER BY observed_at LIMIT 1", (track_id,)).fetchone()
            if not snap:
                return {"kind": t["kind"], "snapshot": None, "previous": None, "features": _fc([])}
            prev = conn.execute(
                "SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s AND observed_at <= %s ORDER BY observed_at DESC LIMIT 1",
                (track_id, snap["observed_at"] - timedelta(days=compare_days) + timedelta(hours=6))).fetchone()
            if prev and prev["id"] == snap["id"]:
                prev = None
            feats = _control_features(snap["id"], prev["id"] if prev and t["kind"] == "control" else None)
            changed = sum(1 for f in feats["features"] if f["properties"].get("changed_from"))
            return {"kind": t["kind"], "snapshot": snap, "previous": prev, "features": feats, "summary": {"changed": changed}}
        if t["kind"] == "front":
            snap = conn.execute(
                "SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s AND observed_at <= %s ORDER BY observed_at DESC LIMIT 1",
                (track_id, at)).fetchone()
            if not snap:  # before the first snapshot: show the first one
                snap = conn.execute("SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s ORDER BY observed_at LIMIT 1",
                                    (track_id,)).fetchone()
            if not snap:
                return {"kind": "front", "snapshot": None, "previous": None, "features": _fc([])}
            prev = conn.execute(
                "SELECT id, observed_at, stats FROM track_snapshots WHERE track_id = %s AND observed_at <= %s ORDER BY observed_at DESC LIMIT 1",
                (track_id, snap["observed_at"] - timedelta(days=compare_days) + timedelta(hours=6))).fetchone()
            if prev and prev["id"] == snap["id"]:
                prev = None
            features = _front_features(snap["id"], prev["id"] if prev else None)
            return {"kind": "front", "snapshot": snap, "previous": prev, "features": features}
        if t["kind"] == "strikes":
            return _strikes(conn, track_id, at, compare_days)
        rows = conn.execute(
            """SELECT id, observed_at, category, label, props, source_url, item_id, confidence, status,
                      ST_Y(geom) AS lat, ST_X(geom) AS lon FROM track_observations
               WHERE track_id = %s AND observed_at <= %s AND status <> 'rejected' AND category = 'position'
               ORDER BY observed_at""", (track_id, at)).fetchall()
    # A movement shows its whole route so far; the trail only fades older reports.
    pts = [{"id": r["id"], "day": r["observed_at"].date(), "lat": r["lat"], "lon": r["lon"], "status": r["status"],
            "confidence": r["confidence"], "report_status": (r["props"] or {}).get("status")} for r in rows]
    path, outliers = daily_path(pts)
    on_path = {p["id"] for p in path}
    feats = []
    if len(path) >= 2:
        feats.append(_feature(json.dumps({"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in path]}),
                              {"category": "path"}))
    for r in rows:
        feats.append(_feature(json.dumps({"type": "Point", "coordinates": [r["lon"], r["lat"]]}), {
            "id": r["id"], "category": "position", "label": r["label"], "observed_at": r["observed_at"].isoformat(),
            "source_url": r["source_url"], "item_id": r["item_id"], "confidence": r["confidence"], "status": r["status"],
            "on_path": r["id"] in on_path, "outlier": r["id"] in outliers,
            "age_days": (at - r["observed_at"]).total_seconds() / 86400, **(r["props"] or {})}))
    latest = path[-1] if path else None
    people = next((r["props"].get("people") for r in reversed(rows) if (r["props"] or {}).get("people")), None)
    distance = sum(km((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(path, path[1:]))
    summary = {"reports": len(rows), "days": len(path), "km": round(distance),
               "latest": {"place": next(r["label"] for r in rows if r["id"] == latest["id"]), "day": latest["day"].isoformat()} if latest else None,
               "people": people}
    return {"kind": t["kind"], "snapshot": None, "previous": None, "summary": summary, "features": _fc(feats)}


def _strikes(conn, track_id: int, at: datetime, days: float) -> dict:
    rows = conn.execute(
        """SELECT o.id, o.observed_at, o.label, o.props, o.source_url, o.status, o.item_id, ST_Y(o.geom) AS lat, ST_X(o.geom) AS lon,
                  (SELECT count(DISTINCT coalesce(x.outlet, x.source_id::text)) FROM items x
                   JOIN sources sx ON sx.id = x.source_id AND sx.kind <> 'telegram'
                   WHERE i.story_id IS NOT NULL AND x.story_id = i.story_id) AS news
           FROM track_observations o LEFT JOIN items i ON i.id = o.item_id
           WHERE o.track_id = %s AND o.category = 'strike' AND o.status <> 'rejected'
             AND o.observed_at > %s AND o.observed_at <= %s""",
        (track_id, at - timedelta(days=days), at)).fetchall()
    photos = photos_for(conn, [r["item_id"] for r in rows if r["item_id"]])
    rows = [{**r, "photos": photos.get(r["item_id"], [])} for r in rows]
    events = group_strikes(rows, at)
    feats = [_feature(json.dumps({"type": "Point", "coordinates": [e["lon"], e["lat"]]}),
                      {"category": "strike", **{k: v for k, v in e.items() if k not in ("lat", "lon")}}) for e in events]
    weapons: dict[str, int] = {}
    for e in events:
        weapons[e["weapon"]] = weapons.get(e["weapon"], 0) + 1
    summary = {"strikes": len(events), "reports": len(rows), "days": days,
               "hits": sum(e["outcome"] == "hit" for e in events),
               "intercepted": sum(e["outcome"] == "intercepted" for e in events),
               "corroborated": sum(e["corroborated"] for e in events),
               "killed": sum(e["killed"] or 0 for e in events), "weapons": weapons}
    return {"kind": "strikes", "snapshot": None, "previous": None, "summary": summary, "features": _fc(feats)}


@router.get("/{track_id}/attacks/{obs_id}")
def attack(track_id: int, obs_id: int) -> dict:
    """One attack arrow, explained: where it points, how long it has been there, ground changing
    hands around it, and strikes and news nearby (tracks/arrows.py)."""
    from .tracks.arrows import attack_info

    with db.connect() as conn:
        info = attack_info(conn, obs_id)
    if not info:
        raise HTTPException(404, "no such arrow")
    return info


class ObservationIn(BaseModel):
    status: str  # confirmed | rejected | auto


@router.post("/observations/{obs_id}")
def set_observation(obs_id: int, body: ObservationIn) -> dict:
    """Confirm a report (it then wins its day on the path) or reject it (it is hidden for good)."""
    if body.status not in ("confirmed", "rejected", "auto"):
        raise HTTPException(400, "status must be confirmed, rejected or auto")
    with db.connect() as conn:
        n = conn.execute("UPDATE track_observations SET status = %s WHERE id = %s", (body.status, obs_id)).rowcount
        conn.commit()
    if not n:
        raise HTTPException(404, "report not found")
    return {"ok": True}


@lru_cache(maxsize=64)
def _front_features(snap_id: int, prev_id: int | None) -> dict:
    """GeoJSON for one front snapshot and its changes. Cached: snapshots never change."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, track_id, category, props, ST_AsGeoJSON(geom, 5) AS g FROM track_observations WHERE snapshot_id = %s
               ORDER BY CASE category WHEN 'liberated' THEN 0 WHEN 'occupied' THEN 1 WHEN 'contested' THEN 2 ELSE 3 END""",
            (snap_id,)).fetchall()
        feats = [_feature(r["g"], {"category": r["category"], **(r["props"] or {}),
                                   **({"id": r["id"], "track_id": r["track_id"]} if r["category"] == "attack" else {})}) for r in rows]
        if prev_id:
            for r in conn.execute(
                """WITH a AS (SELECT geom FROM track_observations WHERE snapshot_id = %(s)s AND category = 'occupied'),
                        b AS (SELECT geom FROM track_observations WHERE snapshot_id = %(p)s AND category = 'occupied'),
                        d AS (SELECT 'gained' AS category, (ST_Dump(ST_Difference(a.geom, b.geom))).geom FROM a, b
                              UNION ALL
                              SELECT 'lost', (ST_Dump(ST_Difference(b.geom, a.geom))).geom FROM a, b)
                   SELECT category, ST_AsGeoJSON(geom, 5) AS g, ST_Area(geom::geography) / 1e6 AS km2 FROM d
                   WHERE ST_Area(geom::geography) >= %(min)s""",
                    {"s": snap_id, "p": prev_id, "min": MIN_CHANGE_M2}):
                feats.append(_feature(r["g"], {"category": r["category"], "km2": round(r["km2"], 2)}))
    return _fc(feats)


@lru_cache(maxsize=128)
def _control_features(snap_id: int, prev_id: int | None) -> dict:
    """A control map (or zones): areas, then places; places that changed hands since `prev_id`
    carry who held them before. Cached: snapshots never change."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, track_id, category, label, props, ST_AsGeoJSON(geom, 4) AS g FROM track_observations
               WHERE snapshot_id = %s ORDER BY CASE category WHEN 'zone' THEN 0 WHEN 'held' THEN 1 ELSE 2 END,
                        (props->>'size')::real NULLS FIRST""", (snap_id,)).fetchall()
        before: dict = {}
        if prev_id:
            for r in conn.execute(
                    """SELECT label, props->>'side' AS side, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM track_observations
                       WHERE snapshot_id = %s AND category = 'place'""", (prev_id,)):
                before[(r["label"], round(r["lon"], 2), round(r["lat"], 2))] = r["side"]
    feats = []
    for r in rows:
        props = {"category": r["category"], "id": r["id"], "track_id": r["track_id"], "label": r["label"], **(r["props"] or {})}
        hexes = props.pop("hexes", None) or []
        props.pop("sides", None)  # lists do not survive the map's tiling; the place card has them
        if hexes:
            props["hex"] = props.get("hex") or hexes[0]
            props["hex2"] = hexes[1] if len(hexes) > 1 else props["hex"]
        if before and r["category"] == "place":
            g = json.loads(r["g"])["coordinates"]
            was = before.get((r["label"], round(g[0], 2), round(g[1], 2)), "?")
            if was != "?" and was != props.get("side") and props.get("side"):
                props["changed_from"] = was or "contested"
        feats.append(_feature(r["g"], props))
    return _fc(feats)


@router.get("/{track_id}/places/{obs_id}")
def place(track_id: int, obs_id: int) -> dict:
    """One place on a control map: who holds it, since when (looking back through the maps), who
    held it before, and strikes and news nearby."""
    from .tracks.arrows import stories_near, strikes_near

    with db.connect() as conn:
        o = conn.execute(
            """SELECT o.id, o.track_id, o.observed_at, o.label, o.props, o.category, ST_Y(ST_PointOnSurface(o.geom)) AS lat,
                      ST_X(ST_PointOnSurface(o.geom)) AS lon, s.stats
               FROM track_observations o JOIN track_snapshots s ON s.id = o.snapshot_id WHERE o.id = %s""", (obs_id,)).fetchone()
        if not o:
            raise HTTPException(404, "no such place")
        side = (o["props"] or {}).get("side")
        history = []
        if o["category"] == "place":
            # The same place on earlier maps: same name, within 3 km.
            for r in conn.execute(
                    """SELECT o2.observed_at, o2.props->>'side' AS side, o2.props->'sides' AS sides FROM track_observations o2
                       WHERE o2.track_id = %s AND o2.category = 'place' AND o2.label = %s AND o2.observed_at <= %s
                         AND ST_DWithin(o2.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 3000)
                       ORDER BY o2.observed_at DESC""", (o["track_id"], o["label"], o["observed_at"], o["lon"], o["lat"])):
                history.append(r)
        since, before = None, None
        for r in history:
            if r["side"] == side:
                since = r["observed_at"]
            else:
                before = r["side"] or ("contested: " + " / ".join(r["sides"] or []))
                break
        oldest = conn.execute("SELECT min(observed_at) AS t FROM track_snapshots WHERE track_id = %s", (o["track_id"],)).fetchone()["t"]
        out = {
            "label": o["label"], "category": o["category"], "side": side, "sides": (o["props"] or {}).get("sides"),
            "kind": (o["props"] or {}).get("kind"), "link": (o["props"] or {}).get("link"),
            "map_date": o["observed_at"].isoformat(), "source_url": (o["stats"] or {}).get("source_url"),
            "since": since.isoformat() if since else None, "at_least": bool(since and oldest and since <= oldest),
            "before": before,
            "strikes": strikes_near(conn, o["lat"], o["lon"], o["observed_at"]),
            "stories": stories_near(conn, o["lat"], o["lon"], o["observed_at"]),
        }
        if o["category"] in ("held", "zone"):
            out["km2"] = round(conn.execute("SELECT ST_Area(geom::geography) / 1e6 AS a FROM track_observations WHERE id = %s",
                                            (obs_id,)).fetchone()["a"])
            out["name"] = (o["props"] or {}).get("name")
    return out


def _feature(geometry_json: str, props: dict) -> dict:
    return {"type": "Feature", "geometry": json.loads(geometry_json), "properties": props}


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}
