"""Tracks for the MAP view: what a front or a movement looked like at a given moment."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from . import db
from .tracks.movement import daily_path, km

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
    `compare_days` earlier ("gained" by the occupier, "lost" by it). Movements: every position
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
            """SELECT category, props, ST_AsGeoJSON(geom, 5) AS g FROM track_observations WHERE snapshot_id = %s
               ORDER BY CASE category WHEN 'liberated' THEN 0 WHEN 'occupied' THEN 1 WHEN 'contested' THEN 2 ELSE 3 END""",
            (snap_id,)).fetchall()
        feats = [_feature(r["g"], {"category": r["category"], **(r["props"] or {})}) for r in rows]
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


def _feature(geometry_json: str, props: dict) -> dict:
    return {"type": "Feature", "geometry": json.loads(geometry_json), "properties": props}


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}
