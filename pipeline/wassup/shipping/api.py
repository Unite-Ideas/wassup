"""The SHIPPING map layers: freight sites, live ships and cargo planes, railways, disruptions."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response

from .. import db
from ..db import kv_get
from . import ais, planes
from .events import KINDS

router = APIRouter(prefix="/api/shipping")

NAV = {0: "under way", 1: "at anchor", 2: "not under command", 3: "restricted in how it can move", 4: "constrained by its draught",
       5: "moored", 6: "aground", 7: "fishing", 8: "under way sailing"}
CARGO = {70: "cargo ship", 71: "cargo ship, hazardous goods (A)", 72: "cargo ship, hazardous goods (B)",
         73: "cargo ship, hazardous goods (C)", 74: "cargo ship, hazardous goods (D)", 80: "tanker",
         81: "tanker, hazardous goods (A)", 82: "tanker, hazardous goods (B)", 83: "tanker, hazardous goods (C)",
         84: "tanker, hazardous goods (D)"}


def ship_kind(t: int | None) -> str:
    if t is None:
        return "unknown"
    return "tanker" if 80 <= t <= 89 else "cargo" if 70 <= t <= 79 else "passenger" if 60 <= t <= 69 else "other"


def ship_label(t: int | None) -> str:
    if t is None:
        return "type not yet known"
    return CARGO.get(t) or {"tanker": "tanker", "cargo": "cargo ship", "passenger": "passenger ship"}.get(ship_kind(t), f"AIS type {t}")


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _pt(lon, lat, props) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]}, "properties": props}


@router.get("/status")
def status() -> dict:
    with db.connect() as conn:
        counts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, count(*) AS n FROM logistics_sites GROUP BY kind")}
        ships = conn.execute(f"SELECT count(*) AS n FROM vessels WHERE {ais.type_sql(ais.wanted_ranges())} AND pos_at > now() - interval '6 hours'").fetchone()["n"]
        out = {
            "ais": {**(kv_get(conn, "ais.status") or {}), "key": bool(ais.api_key()), "ships": ships},
            "planes": {**(kv_get(conn, "planes.status") or {}), "account": bool(planes.account()),
                       "count": conn.execute("SELECT count(*) AS n FROM aircraft").fetchone()["n"]},
            "sites": counts,
            "rail": bool(conn.execute("SELECT 1 FROM rail_lines LIMIT 1").fetchone()),
            "events": conn.execute("SELECT count(*) AS n FROM shipping_events WHERE lat IS NOT NULL AND updated_at > now() - interval '14 days'").fetchone()["n"],
            "kinds": KINDS,
        }
        for part in ("sites", "ports", "chokepoints", "alerts", "airports", "borders"):
            out.setdefault("updated", {})[part] = kv_get(conn, f"shipping.{part}.at")
    return out


@router.get("/sites")
def sites(kinds: str = "port,chokepoint,airport,border") -> dict:
    want = [k for k in kinds.split(",") if k in ("port", "chokepoint", "airport", "border")]
    with db.connect() as conn:
        rows = conn.execute("SELECT id, kind, name, country, lat, lon, rank, stats FROM logistics_sites WHERE kind = ANY(%s) ORDER BY rank DESC",
                            (want,)).fetchall()
    feats = []
    for r in rows:
        s = r["stats"] or {}
        p = {"id": r["id"], "category": f"site-{r['kind']}", "kind": r["kind"], "name": r["name"], "rank": round(r["rank"], 2),
             "unusual": bool(s.get("unusual"))}
        if s.get("change_pct") is not None:
            p["change_pct"] = s["change_pct"]
        if r["kind"] == "border":
            p["delay"] = s.get("truck_delay_min") if s.get("truck_delay_min") is not None else -1
            p["closed"] = (s.get("status") or "").lower() == "closed"
        feats.append(_pt(r["lon"], r["lat"], p))
    return _fc(feats)


@router.get("/sites/{site_id}")
def site(site_id: int) -> dict:
    from ..tracks.arrows import stories_near

    with db.connect() as conn:
        r = conn.execute("SELECT * FROM logistics_sites WHERE id = %s", (site_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such place")
        out = {k: r[k] for k in ("id", "key", "kind", "name", "country", "lat", "lon", "info", "stats")}
        if r["kind"] == "chokepoint":
            out["series"] = [{"day": d["day"].isoformat(), **d["counts"]} for d in conn.execute(
                "SELECT day, counts FROM chokepoint_days WHERE key = %s AND day > now() - interval '400 days' ORDER BY day", (r["key"],))]
        out["events"] = [dict(e, updated_at=e["updated_at"].isoformat()) for e in conn.execute(
            """SELECT id, kind, title, summary, status, severity, story_id, url, updated_at FROM shipping_events
               WHERE lat IS NOT NULL AND updated_at > now() - interval '30 days'
                 AND ST_DWithin(ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
               ORDER BY updated_at DESC LIMIT 6""", (r["lon"], r["lat"], 150_000 if r["kind"] == "chokepoint" else 40_000))]
        out["stories"] = stories_near(conn, r["lat"], r["lon"], datetime.now(timezone.utc))
    return out


def _bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox is west,south,east,north")
    return w, s, e, n


@router.get("/vessels")
def vessels(bbox: str | None = None, limit: int = Query(6000, ge=1, le=30000)) -> dict:
    """Cargo ships and tankers heard from in the last 6 hours, in view. When there are more than
    `limit`, an even sample (the same ships each time, so the map does not flicker)."""
    b = _bbox(bbox)
    where, args = [ais.type_sql(ais.wanted_ranges()), "pos_at > now() - interval '6 hours'"], []
    if b:
        w, s, e, n = b
        where.append("lat BETWEEN %s AND %s")
        args += [s, n]
        if e - w < 360:
            if w <= e:
                where.append("lon BETWEEN %s AND %s")
                args += [w, e]
            else:  # across the date line
                where.append("(lon >= %s OR lon <= %s)")
                args += [w, e]
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT mmsi, name, ship_type, lat, lon, sog, coalesce(heading, cog) AS dir, nav_status FROM vessels
                WHERE {' AND '.join(where)} ORDER BY (mmsi * 2654435761) %% 4294967296 LIMIT %s""", (*args, limit)).fetchall()
    return _fc([_pt(r["lon"], r["lat"], {
        "mmsi": r["mmsi"], "category": "vessel", "name": r["name"] or "", "kind": ship_kind(r["ship_type"]),
        "moving": (r["sog"] or 0) >= 1, "dir": round(r["dir"] or 0)}) for r in rows])


@router.get("/vessels/{mmsi}")
def vessel(mmsi: int, hours: int = Query(48, ge=1, le=168)) -> dict:
    with db.connect() as conn:
        v = conn.execute("SELECT * FROM vessels WHERE mmsi = %s", (mmsi,)).fetchone()
        if not v:
            raise HTTPException(404, "not heard from lately")
        track = conn.execute("""SELECT lat, lon FROM vessel_track WHERE mmsi = %s AND at > now() - make_interval(hours => %s)
                                ORDER BY at""", (mmsi, hours)).fetchall()
    out = {k: v[k] for k in ("mmsi", "name", "imo", "callsign", "ship_type", "length_m", "destination", "eta", "draught",
                             "lat", "lon", "sog", "cog", "heading", "nav_status")}
    out.update(type_label=ship_label(v["ship_type"]), kind=ship_kind(v["ship_type"]), nav=NAV.get(v["nav_status"]),
               pos_at=v["pos_at"].isoformat() if v["pos_at"] else None,
               track=[[round(t["lon"], 4), round(t["lat"], 4)] for t in track] + ([[round(v["lon"], 4), round(v["lat"], 4)]] if v["lat"] is not None else []),
               links={"marinetraffic": f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
                      "vesselfinder": f"https://www.vesselfinder.com/vessels/details/{mmsi}"})
    return out


@router.get("/planes")
def aircraft() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM aircraft WHERE NOT on_ground ORDER BY icao24").fetchall()
    return _fc([_pt(r["lon"], r["lat"], {
        "icao24": r["icao24"], "category": "plane", "callsign": r["callsign"], "operator": r["operator"],
        "country": r["origin_country"], "alt_m": round(r["alt_m"] or 0), "speed_kt": round(r["speed_kt"] or 0),
        "dir": round(r["track"] or 0), "seen_at": r["seen_at"].isoformat(),
        "link": f"https://globe.adsbexchange.com/?icao={r['icao24']}"}) for r in rows])


@router.get("/events")
def events(days: int = Query(14, ge=1, le=90)) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT e.*, coalesce(s.item_count, e.items) AS articles FROM shipping_events e LEFT JOIN stories s ON s.id = e.story_id
               WHERE e.lat IS NOT NULL AND e.kind <> 'none' AND coalesce(e.ended_at, e.updated_at) > now() - make_interval(days => %s)
               ORDER BY e.severity DESC, e.updated_at DESC""", (days,)).fetchall()
    return _fc([_pt(r["lon"], r["lat"], {
        "id": r["id"], "category": "disruption", "kind": r["kind"], "kind_label": KINDS.get(r["kind"], r["kind"]),
        "title": r["title"], "summary": r["summary"] or "", "place": r["place"] or "", "severity": r["severity"],
        "status": r["status"], "source": r["source"], "story_id": r["story_id"] or 0, "url": r["url"] or "",
        "articles": r["articles"] or 0, "info": r["info"] or {},
        "updated_at": r["updated_at"].isoformat(), "started_at": r["started_at"].isoformat() if r["started_at"] else ""}) for r in rows])


@router.get("/tiles/rail/{z}/{x}/{y}.mvt")
def rail_tile(z: int, x: int, y: int) -> Response:
    if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404)
    # Simplify to about a pixel at this zoom, so a whole continent stays light.
    tolerance = 40075016.0 / (256 * 2 ** z)
    with db.connect() as conn:
        row = conn.execute(
            """WITH b AS (SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS env),
                    t AS (SELECT ST_AsMVTGeom(ST_Simplify(r.geom, %(tol)s), b.env, 4096, 64, true) AS geom
                          FROM rail_lines r, b WHERE r.geom && b.env)
               SELECT ST_AsMVT(t, 'rail', 4096, 'geom') AS tile FROM t WHERE geom IS NOT NULL""",
            {"z": z, "x": x, "y": y, "tol": tolerance}).fetchone()
    return Response(bytes(row["tile"] or b""), media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "public, max-age=86400"})

