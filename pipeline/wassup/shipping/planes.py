"""Cargo planes in the air, from the OpenSky Network (https://opensky-network.org).

One look at every aircraft OpenSky sees, worldwide, keeping those whose callsign starts with a
cargo airline's code (config/shipping.yaml). Without an account OpenSky allows about one global
look every 15 minutes; with a free account (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET in .env,
from your account page) every 2 minutes.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from ..db import kv_get, kv_set
from . import cfg

log = logging.getLogger(__name__)

STATES = "https://opensky-network.org/api/states/all"
TOKEN = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
MS_TO_KT = 1.943844
_token: dict = {}


def account() -> tuple[str, str] | None:
    cid, secret = os.environ.get("OPENSKY_CLIENT_ID", "").strip(), os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def _headers(client: httpx.Client) -> dict:
    acc = account()
    if not acc:
        return {}
    if _token.get("expires", 0) < time.time() + 60:
        r = client.post(TOKEN, data={"grant_type": "client_credentials", "client_id": acc[0], "client_secret": acc[1]})
        r.raise_for_status()
        d = r.json()
        _token.update(value=d["access_token"], expires=time.time() + int(d.get("expires_in", 1800)))
    return {"Authorization": f"Bearer {_token['value']}"}


def cargo_states(states: list[list], airlines: dict[str, str]) -> list[tuple]:
    """Rows for the aircraft table: those flying for a cargo airline, with a position."""
    out = []
    for s in states or []:
        callsign = (s[1] or "").strip().upper()
        operator = airlines.get(callsign[:3])
        if not operator or s[5] is None or s[6] is None:
            continue
        seen = datetime.fromtimestamp(s[4] or s[3] or time.time(), timezone.utc)
        alt = s[13] if s[13] is not None else s[7]
        out.append((s[0], callsign, operator, s[2], s[6], s[5], alt, (s[9] or 0) * MS_TO_KT, s[10], bool(s[8]), seen))
    return out


def run_planes(conn: psycopg.Connection, client: httpx.Client | None = None, force: bool = False) -> int:
    c = cfg().get("planes") or {}
    if not c.get("enabled", True):
        return 0
    every = timedelta(minutes=float(c.get("every_minutes_with_account" if account() else "every_minutes", 15)))
    last = kv_get(conn, "planes.at")
    now = datetime.now(timezone.utc)
    if not force and last and now - datetime.fromisoformat(last) < every:
        return 0
    kv_set(conn, "planes.at", now.isoformat())
    conn.commit()
    client = client or httpx.Client(timeout=90, follow_redirects=True)
    try:
        r = client.get(STATES, headers=_headers(client))
        if r.status_code == 429:
            log.info("cargo planes: OpenSky's daily allowance is used up; trying again later")
            kv_set(conn, "planes.status", {"ok": False, "error": "OpenSky's daily allowance is used up", "at": now.isoformat()})
            conn.commit()
            return 0
        r.raise_for_status()
        rows = cargo_states(r.json().get("states") or [], {k.upper(): v for k, v in (c.get("cargo_airlines") or {}).items()})
    except Exception as e:  # noqa: BLE001
        kv_set(conn, "planes.status", {"ok": False, "error": str(e)[:300], "at": now.isoformat()})
        conn.commit()
        raise
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO aircraft (icao24, callsign, operator, origin_country, lat, lon, alt_m, speed_kt, track, on_ground, seen_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (icao24) DO UPDATE SET callsign = EXCLUDED.callsign, operator = EXCLUDED.operator, lat = EXCLUDED.lat,
                 lon = EXCLUDED.lon, alt_m = EXCLUDED.alt_m, speed_kt = EXCLUDED.speed_kt, track = EXCLUDED.track,
                 on_ground = EXCLUDED.on_ground, seen_at = EXCLUDED.seen_at""", rows)
    # Planes OpenSky no longer sees have landed or flown out of its coverage.
    conn.execute("DELETE FROM aircraft WHERE seen_at < %s", (now - 2 * every - timedelta(minutes=5),))
    kv_set(conn, "planes.status", {"ok": True, "planes": len(rows), "account": bool(account()), "at": now.isoformat()})
    conn.commit()
    return len(rows)
