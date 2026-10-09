"""Official zones: the lines that govern daily life, like the West Bank's Oslo areas A, B and C
(config/conflicts.yaml, `zones:`). Downloaded from the Humanitarian Data Exchange
(data.humdata.org) as shapefiles, refreshed monthly, drawn as shaded areas.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..config import load_yaml

log = logging.getLogger(__name__)

HDX = "https://data.humdata.org/api/3/action/package_show"
REFRESH = timedelta(days=30)
# Colours for the West Bank's zones; anything else gets a neutral grey.
ZONE_COLORS = {"a": "#22b04a", "b": "#f5d90a", "c": "#2f7bf0", "h1": "#22b04a", "h2": "#2f7bf0",
               "israeli declared east jerusalem": "#3b4fc4", "nature reserve": "#14a89c", "no man's land": "#a3abb3"}
ZONE_NAMES = {"a": "Area A: Palestinian Authority civil and security control",
              "b": "Area B: Palestinian civil control, Israeli security control",
              "c": "Area C: Israeli civil and security control",
              "h1": "H1 (Hebron): Palestinian Authority", "h2": "H2 (Hebron): Israeli military control",
              "israeli declared east jerusalem": "East Jerusalem, annexed by Israel",
              "nature reserve": "Nature reserve (Wye River Memorandum)", "no man's land": "No man's land (1949 armistice)"}


def _srid(prj: str) -> int:
    if "Palestine_1923" in prj:
        return 28191
    if "Israel" in prj and "TM" in prj:
        return 2039
    return 4326


def load_zone(conn: psycopg.Connection, z: dict, client: httpx.Client | None = None) -> int:
    client = client or httpx.Client(timeout=120, follow_redirects=True)
    track = conn.execute(
        """INSERT INTO tracks (key, name, kind, desk, source, description, meta) VALUES (%s, %s, 'zones', %s, %s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, desk = EXCLUDED.desk, description = EXCLUDED.description,
             meta = EXCLUDED.meta RETURNING id""",
        (f"zones-{z['key']}", z["name"], z.get("desk"), "Humanitarian Data Exchange", z.get("note"),
         Jsonb({"hdx": z["hdx"], "url": f"https://data.humdata.org/dataset/{z['hdx']}"}))).fetchone()["id"]
    last = conn.execute("SELECT max(created_at) AS t FROM track_snapshots s JOIN track_observations o ON o.snapshot_id = s.id "
                        "WHERE s.track_id = %s", (track,)).fetchone()["t"]
    conn.commit()
    if last and datetime.now(timezone.utc) - last < REFRESH:
        return 0
    pkg = client.get(HDX, params={"id": z["hdx"]}).json()["result"]
    res = next(r for r in pkg["resources"] if (r.get("format") or "").upper() in ("SHP", "ZIP", "ZIPPED SHAPEFILE"))
    data = client.get(res["url"]).content
    import shapefile

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    stem = next(n[:-4] for n in names if n.lower().endswith(".shp"))
    prj = zf.read(stem + ".prj").decode(errors="ignore") if stem + ".prj" in names else ""
    reader = shapefile.Reader(shp=io.BytesIO(zf.read(stem + ".shp")), dbf=io.BytesIO(zf.read(stem + ".dbf")),
                              shx=io.BytesIO(zf.read(stem + ".shx")))
    field = z.get("field") or reader.fields[1][0]
    observed = datetime.fromisoformat((pkg.get("last_modified") or pkg.get("metadata_modified")).split(".")[0]).replace(tzinfo=timezone.utc)
    ref = f"{z['hdx']}@{res.get('last_modified') or pkg.get('metadata_modified')}"
    conn.execute("DELETE FROM track_snapshots WHERE track_id = %s", (track,))
    counts: dict[str, int] = {}
    snap = conn.execute("INSERT INTO track_snapshots (track_id, observed_at, source_ref, stats) VALUES (%s, %s, %s, '{}') RETURNING id",
                        (track, observed, ref)).fetchone()["id"]
    import json
    for sr in reader.iterShapeRecords():
        value = str(sr.record[field]).strip()
        key = value.lower()
        geo = json.dumps(sr.shape.__geo_interface__)
        conn.execute(
            """INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom, label, props)
               VALUES (%s, %s, %s, 'zone', ST_Multi(ST_MakeValid(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), %s), 4326))), %s, %s)""",
            (track, snap, observed, geo, _srid(prj), value,
             Jsonb({"zone": value, "name": ZONE_NAMES.get(key, value), "hex": ZONE_COLORS.get(key, "#a3abb3")})))
        counts[value] = counts.get(value, 0) + 1
    areas = {r["label"]: round(r["km2"]) for r in conn.execute(
        "SELECT label, sum(ST_Area(geom::geography)) / 1e6 AS km2 FROM track_observations WHERE snapshot_id = %s GROUP BY label", (snap,))}
    legend = {v: {"hex": ZONE_COLORS.get(v.lower(), "#a3abb3"), "name": ZONE_NAMES.get(v.lower(), v), "km2": areas.get(v), "parts": n}
              for v, n in counts.items()}
    conn.execute("UPDATE track_snapshots SET stats = %s WHERE id = %s",
                 (Jsonb({"zones": legend, "source_url": f"https://data.humdata.org/dataset/{z['hdx']}",
                         "dataset": pkg.get("title"), "publisher": pkg.get("dataset_source")}), snap))
    conn.commit()
    log.info("zones: %s loaded (%d areas)", z["name"], sum(counts.values()))
    return 1


def run_zones(conn: psycopg.Connection) -> int:
    done = 0
    for z in load_yaml("conflicts.yaml").get("zones") or []:
        try:
            done += load_zone(conn, z)
        except Exception as e:
            conn.rollback()
            log.warning("zones: could not load %s: %s", z.get("name"), e)
    return done


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
