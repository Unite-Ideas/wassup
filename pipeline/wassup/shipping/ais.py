"""Live ships from AISStream (https://aisstream.io), a free feed of ships' AIS radio messages.

One websocket for the whole world (or the boxes in config/shipping.yaml). Every ship sends its
position every few seconds to minutes and its identity (name, type, destination) every six
minutes. Positions are kept in memory and written in one batch every few seconds: the latest
per ship in `vessels`, and every 15 minutes a point on each cargo ship's and tanker's trail in
`vessel_track`. Ships of other types (fishing, passenger, pleasure) are dropped once their type
is known.

Needs AISSTREAM_API_KEY in .env. Without it nothing runs and the map says how to add one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import db
from ..db import kv_set
from . import cfg

log = logging.getLogger(__name__)

URL = "wss://stream.aisstream.io/v0/stream"
FLUSH_S = 10
_thread: threading.Thread | None = None


def api_key() -> str | None:
    return (os.environ.get("AISSTREAM_API_KEY") or "").strip() or None


def wanted_ranges() -> list[tuple[int, int]]:
    return [tuple(r) for r in (cfg().get("ais") or {}).get("ship_types") or [[70, 79], [80, 89]]]


def wanted(ship_type: int | None, ranges: list[tuple[int, int]]) -> bool:
    return ship_type is not None and any(a <= ship_type <= b for a, b in ranges)


def type_sql(ranges: list[tuple[int, int]]) -> str:
    """SQL condition for the wanted ship types (numbers from config only, never from users)."""
    return "(" + " OR ".join(f"ship_type BETWEEN {int(a)} AND {int(b)}" for a, b in ranges) + ")"


def _time(meta: dict) -> datetime:
    t = (meta or {}).get("time_utc") or ""
    try:
        # "2026-10-09 18:11:38.123456789 +0000 UTC"
        return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _clean(s) -> str | None:
    s = " ".join(str(s or "").replace("@", " ").split())
    return s or None


class Buffer:
    """What arrived since the last write, merged per ship."""

    def __init__(self, ranges: list[tuple[int, int]]):
        self.ranges = ranges
        self.positions: dict[int, tuple] = {}
        self.statics: dict[int, dict] = {}
        self.unwanted: set[int] = set()   # ships of other types: positions skipped
        self.to_delete: set[int] = set()  # and removed from the table at the next write
        self.messages = 0
        self.last_at: datetime | None = None

    def add(self, msg: dict) -> None:
        kind = msg.get("MessageType")
        body = (msg.get("Message") or {}).get(kind) or {}
        meta = msg.get("MetaData") or {}
        mmsi = int(meta.get("MMSI") or body.get("UserID") or 0)
        if not mmsi:
            return
        self.messages += 1
        at = _time(meta)
        self.last_at = at
        if kind == "ShipStaticData":
            t = body.get("Type")
            if t is not None and not wanted(int(t), self.ranges):
                if mmsi not in self.unwanted:
                    self.unwanted.add(mmsi)
                    self.to_delete.add(mmsi)
                self.positions.pop(mmsi, None)
                return
            self.unwanted.discard(mmsi)
            dim = body.get("Dimension") or {}
            eta = body.get("Eta") or {}
            self.statics[mmsi] = {
                "name": _clean(body.get("Name") or meta.get("ShipName")), "imo": int(body.get("ImoNumber") or 0) or None,
                "callsign": _clean(body.get("CallSign")), "ship_type": int(t) if t is not None else None,
                "length_m": (int(dim.get("A") or 0) + int(dim.get("B") or 0)) or None,
                "destination": _clean(body.get("Destination")),
                "eta": f"{eta.get('Month', 0):02d}-{eta.get('Day', 0):02d} {eta.get('Hour', 0):02d}:{eta.get('Minute', 0):02d}" if eta.get("Month") else None,
                "draught": body.get("MaximumStaticDraught") or None, "at": at}
        elif kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
            if mmsi in self.unwanted:
                return
            lat, lon = body.get("Latitude"), body.get("Longitude")
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
                return
            heading = body.get("TrueHeading")
            self.positions[mmsi] = (lat, lon, body.get("Sog"), body.get("Cog"), heading if heading != 511 else None,
                                    body.get("NavigationalStatus"), at, _clean(meta.get("ShipName")))

    def take(self) -> tuple[dict, dict, set]:
        p, s, d = self.positions, self.statics, self.to_delete
        self.positions, self.statics, self.to_delete = {}, {}, set()
        return p, s, d


def flush(conn, positions: dict, statics: dict, drop: set) -> None:
    with conn.cursor() as cur:
        if statics:
            cur.executemany(
                """INSERT INTO vessels (mmsi, name, imo, callsign, ship_type, length_m, destination, eta, draught, static_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (mmsi) DO UPDATE SET name = coalesce(EXCLUDED.name, vessels.name), imo = coalesce(EXCLUDED.imo, vessels.imo),
                     callsign = coalesce(EXCLUDED.callsign, vessels.callsign), ship_type = EXCLUDED.ship_type,
                     length_m = coalesce(EXCLUDED.length_m, vessels.length_m), destination = EXCLUDED.destination, eta = EXCLUDED.eta,
                     draught = EXCLUDED.draught, static_at = EXCLUDED.static_at""",
                [(m, s["name"], s["imo"], s["callsign"], s["ship_type"], s["length_m"], s["destination"], s["eta"], s["draught"], s["at"])
                 for m, s in sorted(statics.items())])
        if positions:
            cur.executemany(
                """INSERT INTO vessels (mmsi, name, lat, lon, sog, cog, heading, nav_status, pos_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (mmsi) DO UPDATE SET name = coalesce(vessels.name, EXCLUDED.name), lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                     sog = EXCLUDED.sog, cog = EXCLUDED.cog, heading = EXCLUDED.heading, nav_status = EXCLUDED.nav_status,
                     pos_at = EXCLUDED.pos_at""",
                [(m, p[7], p[0], p[1], p[2], p[3], p[4], p[5], p[6]) for m, p in sorted(positions.items())])
        if drop:
            cur.execute("DELETE FROM vessels WHERE mmsi = ANY(%s)", (sorted(drop),))
    conn.commit()


def upkeep(conn) -> None:
    """Points on the trails every 15 minutes, and forget what is old."""
    c = cfg().get("ais") or {}
    every = int(c.get("track_every_minutes", 15))
    types = type_sql(wanted_ranges())
    conn.execute(f"""INSERT INTO vessel_track (mmsi, at, lat, lon, sog)
                     SELECT mmsi, pos_at, lat, lon, sog FROM vessels v
                     WHERE {types} AND pos_at > now() - make_interval(mins => %s)
                       AND NOT EXISTS (SELECT 1 FROM vessel_track t WHERE t.mmsi = v.mmsi AND t.at > now() - make_interval(mins => %s))
                     ON CONFLICT DO NOTHING""", (every, every))
    conn.execute("DELETE FROM vessel_track WHERE at < now() - make_interval(days => %s)", (int(c.get("keep_track_days", 7)),))
    conn.execute("DELETE FROM vessels WHERE coalesce(pos_at, static_at) < now() - make_interval(days => %s)",
                 (int(c.get("forget_after_days", 3)),))
    conn.commit()


def _status(**s) -> None:
    try:
        with db.connect() as conn:
            kv_set(conn, "ais.status", {**s, "at": datetime.now(timezone.utc).isoformat()})
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


async def _run(key: str, stop: threading.Event) -> None:
    import websockets

    c = cfg().get("ais") or {}
    boxes = c.get("boxes") or [[[-90, -180], [90, 180]]]
    buf = Buffer(wanted_ranges())
    loop = asyncio.get_running_loop()
    backoff = 5
    last_upkeep = 0.0
    while not stop.is_set():
        try:
            async with websockets.connect(URL, max_size=2 ** 22, ping_interval=30, open_timeout=30) as ws:
                await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": boxes,
                                          "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}))
                log.info("AIS: connected to AISStream")
                backoff = 5
                last_flush = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=FLUSH_S)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        msg = json.loads(raw)
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        buf.add(msg)
                    if time.time() - last_flush >= FLUSH_S:
                        last_flush = time.time()
                        await loop.run_in_executor(None, _write, *buf.take())
                        if time.time() - last_upkeep >= 60:
                            last_upkeep = time.time()
                            await loop.run_in_executor(None, _upkeep_and_status, buf)
        except Exception as e:  # noqa: BLE001
            text = str(e)
            log.warning("AIS: connection lost (%s); reconnecting in %d s", text[:200], backoff)
            _status(connected=False, error=text[:300], messages=buf.messages)
            await asyncio.sleep(backoff + random.random() * 3)
            # A wrong key closes the socket at once; do not hammer the service.
            backoff = min(600, backoff * 2)


def _write(positions: dict, statics: dict, drop: set) -> None:
    try:
        with db.connect() as conn:
            flush(conn, positions, statics, drop)
    except Exception as e:  # noqa: BLE001
        log.warning("AIS: could not save positions: %s", e)


def _upkeep_and_status(buf: Buffer) -> None:
    try:
        with db.connect() as conn:
            upkeep(conn)
            n = conn.execute(f"SELECT count(*) AS n FROM vessels WHERE {type_sql(buf.ranges)} AND pos_at > now() - interval '1 hour'").fetchone()["n"]
            kv_set(conn, "ais.status", {"connected": True, "messages": buf.messages, "ships_last_hour": n,
                                        "last_message": buf.last_at.isoformat() if buf.last_at else None,
                                        "at": datetime.now(timezone.utc).isoformat()})
            conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("AIS: upkeep failed: %s", e)


def start_ais(stop: threading.Event | None = None) -> bool:
    """Start the reader thread if there is a key and AIS is on. Safe to call twice."""
    global _thread
    key = api_key()
    if not key or not (cfg().get("ais") or {}).get("enabled", True):
        _status(connected=False, error=None if key else "no key")
        return False
    if _thread and _thread.is_alive():
        return True
    stop = stop or threading.Event()
    _thread = threading.Thread(target=lambda: asyncio.run(_run(key, stop)), name="ais-reader", daemon=True)
    _thread.start()
    return True
