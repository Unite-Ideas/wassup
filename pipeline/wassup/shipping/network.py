"""The fixed freight network and its daily numbers.

- Ports (2,000+) and chokepoints (28 straits and canals) from IMF PortWatch, which counts ships
  from satellite AIS. Each port gets its port calls this week against its normal week; each
  chokepoint its daily transits, kept for a trend line. PortWatch runs about a week behind.
- PortWatch's alerts: storms, quakes and floods that hit ports (GDACS), as shipping events.
- Airports with scheduled service (OurAirports), for where cargo planes land.
- Freight railways (Natural Earth), loaded once into the database and drawn as map tiles.
- US land border crossings with commercial truck wait times (CBP), every 15 minutes.

Each part refreshes on its own clock (DUE), so one scheduler step every few minutes is enough.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import zipfile
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..db import kv_get, kv_set
from . import cfg

log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parents[1] / "data"
UA = "WassupNewsBot/0.1 (personal news research; https://github.com/Unite-Ideas/wassup)"
DUE = {"sites": timedelta(days=7), "ports": timedelta(hours=12), "chokepoints": timedelta(hours=6),
       "alerts": timedelta(hours=6), "airports": timedelta(days=30), "rail": timedelta(days=3650),
       "borders": timedelta(minutes=15)}
AIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RAIL_ZIP = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_railroads.zip"
PORTWATCH_PAGE = "https://portwatch.imf.org/pages/{}"
NORMAL_WEEKS = 8   # a port's normal week: the average of the 8 weeks before this one
# PortWatch's alert types (from GDACS).
HAZARDS = {"EQ": "earthquake", "TC": "tropical cyclone", "FL": "flood", "WF": "wildfire", "VO": "volcano", "DR": "drought", "TS": "tsunami"}


@lru_cache
def iso3_to_iso2() -> dict[str, str]:
    with open(DATA / "countries.tsv", encoding="utf-8") as fh:
        return {r["iso3"]: r["iso2"] for r in csv.DictReader(fh, delimiter="\t") if r["iso3"]}


def _client() -> httpx.Client:
    return httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": UA})


def _due(conn, part: str, now: datetime) -> bool:
    last = kv_get(conn, f"shipping.{part}.at")
    return not last or now - datetime.fromisoformat(last) >= DUE[part]


def _done(conn, part: str, now: datetime) -> None:
    kv_set(conn, f"shipping.{part}.at", now.isoformat())
    conn.commit()


def arcgis(client: httpx.Client, service: str, limit: int | None = None, **params) -> list[dict]:
    """Every row of a PortWatch layer query (or the first `limit`), page by page."""
    url = f"{cfg().get('portwatch', {}).get('base')}/{service}/FeatureServer/0/query"
    out: list[dict] = []
    offset = 0
    while True:
        q = {"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json",
             "resultOffset": offset, "resultRecordCount": min(1000, limit or 1000), **params}
        d = client.get(url, params={k: v for k, v in q.items() if v is not None}).json()
        if "error" in d:
            raise RuntimeError(f"PortWatch {service}: {d['error']}")
        rows = [f["attributes"] for f in d.get("features") or []]
        out += rows
        if not d.get("exceededTransferLimit") or not rows or (limit and len(out) >= limit):
            return out[:limit] if limit else out
        offset += len(rows)


def _upsert_sites(conn, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO logistics_sites (key, kind, name, country, lat, lon, rank, info, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind, name = EXCLUDED.name, country = EXCLUDED.country,
                 lat = EXCLUDED.lat, lon = EXCLUDED.lon, rank = EXCLUDED.rank, info = EXCLUDED.info, updated_at = now()""", rows)


# ---- PortWatch: ports and chokepoints ----

def load_sites(conn, client) -> int:
    iso = iso3_to_iso2()
    rows = []
    for service, kind in (("PortWatch_ports_database", "port"), ("PortWatch_chokepoints_database", "chokepoint")):
        for a in arcgis(client, service):
            if a.get("lat") is None or a.get("lon") is None:
                continue
            counts = {k.removeprefix("vessel_count_").lower(): a.get(k) for k in a if k.startswith("vessel_count_")}
            info = {"locode": a.get("LOCODE"), "full_name": a.get("fullname"), "industries": [a.get(f"industry_top{i}") for i in (1, 2, 3) if a.get(f"industry_top{i}")],
                    "ships_per_year": counts, "share_of_imports": a.get("share_country_maritime_import"),
                    "share_of_exports": a.get("share_country_maritime_export"),
                    "url": PORTWATCH_PAGE.format(a["pageid"]) if a.get("pageid") else None, "portid": a["portid"]}
            rank = math.log10(1 + (a.get("vessel_count_total") or 0))
            rows.append((f"pw:{a['portid']}", kind, a.get("portname") or a["portid"], iso.get(a.get("ISO3") or ""),
                         a["lat"], a["lon"], rank + (2 if kind == "chokepoint" else 0), Jsonb(info)))
    _upsert_sites(conn, rows)
    conn.commit()
    return len(rows)


def latest_day(client, service: str) -> date:
    """The newest day in a PortWatch daily table (a statistic, so the service does not sort millions of rows)."""
    url = f"{cfg().get('portwatch', {}).get('base')}/{service}/FeatureServer/0/query"
    d = client.get(url, params={"where": "1=1", "f": "json", "outStatistics":
                                '[{"statisticType":"max","onStatisticField":"date","outStatisticFieldName":"d"}]'}).json()
    if "error" in d:
        raise RuntimeError(f"PortWatch {service}: {d['error']}")
    return date.fromisoformat(str(d["features"][0]["attributes"]["d"])[:10])


def port_stats(conn, client) -> int:
    """Port calls, imports and exports this week against the port's normal week."""
    if not conn.execute("SELECT 1 FROM logistics_sites WHERE kind = 'port' LIMIT 1").fetchone():
        raise RuntimeError("no ports loaded yet")
    end = latest_day(client, "Daily_Ports_Data")
    week0 = end - timedelta(days=6)
    base0 = week0 - timedelta(days=7 * NORMAL_WEEKS)
    stats = '[{"statisticType":"sum","onStatisticField":"portcalls","outStatisticFieldName":"calls"},' \
            '{"statisticType":"sum","onStatisticField":"import","outStatisticFieldName":"imp"},' \
            '{"statisticType":"sum","onStatisticField":"export","outStatisticFieldName":"exp"}]'

    def sums(a: date, b: date) -> dict[str, dict]:
        rows = arcgis(client, "Daily_Ports_Data", where=f"date BETWEEN '{a}' AND '{b}'", groupByFieldsForStatistics="portid",
                      outStatistics=stats, orderByFields="portid", outFields=None)
        return {r["portid"]: r for r in rows}

    week, base = sums(week0, end), sums(base0, week0 - timedelta(days=1))
    rows = []
    for pid in set(week) | set(base):
        w, b = week.get(pid, {}), base.get(pid, {})
        normal = (b.get("calls") or 0) / NORMAL_WEEKS
        calls = w.get("calls") or 0
        rows.append((Jsonb(port_change({
            "as_of": end.isoformat(), "calls_week": calls, "calls_normal": round(normal, 1),
            "import_t": w.get("imp") or 0, "export_t": w.get("exp") or 0,
            "import_normal_t": round((b.get("imp") or 0) / NORMAL_WEEKS), "export_normal_t": round((b.get("exp") or 0) / NORMAL_WEEKS)})),
            f"pw:{pid}"))
    with conn.cursor() as cur:
        cur.executemany("UPDATE logistics_sites SET stats = %s, updated_at = now() WHERE key = %s", rows)
    conn.commit()
    return len(rows)


def port_change(s: dict) -> dict:
    """How unusual this week is: the change in calls, flagged when the port is busy enough for it to mean something."""
    normal, calls = s["calls_normal"], s["calls_week"]
    s["change_pct"] = round(100 * (calls - normal) / normal) if normal >= 1 else None
    # A quiet port going from 2 calls to 0 is noise; a port with 20 a week going to 8 is news.
    s["unusual"] = bool(normal >= 7 and s["change_pct"] is not None and abs(s["change_pct"]) >= 40)
    return s


COUNT_FIELDS = {"n_total": "total", "n_container": "container", "n_tanker": "tanker", "n_dry_bulk": "dry_bulk",
                "n_general_cargo": "general_cargo", "n_roro": "roro", "capacity": "capacity"}


def chokepoint_days(conn, client) -> int:
    """Daily transits through each chokepoint, a year back the first time, then what is new."""
    last = conn.execute("SELECT max(day) AS d FROM chokepoint_days").fetchone()["d"]
    since = (last - timedelta(days=7)) if last else date.today() - timedelta(days=400)
    rows = arcgis(client, "Daily_Chokepoints_Data", where=f"date >= '{since}'", orderByFields="date,portid",
                  outFields="date,portid," + ",".join(COUNT_FIELDS))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO chokepoint_days (key, day, counts) VALUES (%s, %s, %s)
               ON CONFLICT (key, day) DO UPDATE SET counts = EXCLUDED.counts""",
            [(f"pw:{r['portid']}", r["date"][:10], Jsonb({v: r.get(k) or 0 for k, v in COUNT_FIELDS.items()})) for r in rows])
    conn.commit()
    chokepoint_stats(conn)
    return len(rows)


def chokepoint_stats(conn) -> None:
    """This week's transits against the 8 weeks before and the same week a year ago."""
    for r in conn.execute("SELECT DISTINCT key FROM chokepoint_days").fetchall():
        days = conn.execute("SELECT day, counts FROM chokepoint_days WHERE key = %s AND day > now() - interval '420 days' ORDER BY day",
                            (r["key"],)).fetchall()
        if not days:
            continue
        end = days[-1]["day"]
        by = {d["day"]: d["counts"] for d in days}

        def total(a: date, b: date, field="total") -> tuple[int, int]:
            vals = [by[d][field] for d in (a + timedelta(days=i) for i in range((b - a).days + 1)) if d in by]
            return sum(vals), len(vals)

        week, wn = total(end - timedelta(days=6), end)
        base, bn = total(end - timedelta(days=6 + 7 * NORMAL_WEEKS), end - timedelta(days=7))
        ly, lyn = total(end - timedelta(days=365 + 6), end - timedelta(days=365))
        tankers, _ = total(end - timedelta(days=6), end, "tanker")
        normal = base / bn * 7 if bn else None
        s = {"as_of": end.isoformat(), "transits_week": week, "transits_normal": round(normal) if normal else None,
             "tankers_week": tankers, "transits_last_year": ly if lyn >= 5 else None,
             "change_pct": round(100 * (week - normal) / normal) if normal else None,
             "vs_last_year_pct": round(100 * (week - ly) / ly) if ly and lyn >= 5 else None}
        s["unusual"] = bool(s["change_pct"] is not None and abs(s["change_pct"]) >= 25 and wn >= 5)
        conn.execute("UPDATE logistics_sites SET stats = %s, updated_at = now() WHERE key = %s", (Jsonb(s), r["key"]))
    conn.commit()


def alerts(conn, client) -> int:
    """PortWatch's natural hazard alerts near ports, as shipping events while they last."""
    rows = arcgis(client, "portwatch_disruptions_database")
    names = {r["key"].removeprefix("pw:"): r["name"] for r in conn.execute("SELECT key, name FROM logistics_sites WHERE kind = 'port'")}
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    n = 0
    for a in rows:
        start = datetime.fromtimestamp(a["fromdate"] / 1000, timezone.utc) if a.get("fromdate") else None
        end = datetime.fromtimestamp(a["todate"] / 1000, timezone.utc) if a.get("todate") else start
        if not end or end < cutoff or a.get("lat") is None:
            continue
        ports = [names.get(p.strip(), p.strip()) for p in (a.get("affectedports") or "").split(",") if p.strip()]
        hazard = HAZARDS.get(a.get("eventtype") or "", "natural hazard")
        severity = {"RED": 3, "ORANGE": 2}.get((a.get("alertlevel") or "").upper(), 1)
        conn.execute(
            """INSERT INTO shipping_events (key, kind, title, summary, place, country, lat, lon, severity, status, source, url,
                                            started_at, ended_at, info, updated_at)
               VALUES (%s, 'hazard', %s, %s, %s, NULL, %s, %s, %s, %s, 'portwatch', %s, %s, %s, %s, now())
               ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, summary = EXCLUDED.summary, severity = EXCLUDED.severity,
                 status = EXCLUDED.status, ended_at = EXCLUDED.ended_at, info = EXCLUDED.info, updated_at = now()""",
            (f"pw:{a['eventid']}", a.get("eventname") or hazard.capitalize(), _plain(a.get("htmldescription")), a.get("country"),
             a["lat"], a["long"], severity, "ended" if end < datetime.now(timezone.utc) - timedelta(days=2) else "ongoing",
             "https://portwatch.imf.org/pages/port-monitor", start, end,
             Jsonb({"hazard": hazard, "alert": a.get("alertlevel"), "ports": ports[:20], "ports_hit": a.get("n_affectedports") or 0,
                    "people": a.get("affectedpopulation")})))
        n += 1
    conn.commit()
    return n


def _plain(html: str | None) -> str | None:
    if not html:
        return None
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:500]


# ---- Airports ----

def load_airports(conn, client) -> int:
    text = client.get(AIRPORTS_CSV).text
    rows = []
    for a in csv.DictReader(io.StringIO(text)):
        big = a["type"] == "large_airport"
        if not (big or (a["type"] == "medium_airport" and a["scheduled_service"] == "yes")):
            continue
        code = a["icao_code"] or a["gps_code"] or a["ident"]
        try:
            lat, lon = float(a["latitude_deg"]), float(a["longitude_deg"])
        except ValueError:
            continue
        rows.append((f"oa:{code}", "airport", a["name"], a["iso_country"] or None, lat, lon, 2 if big else 1,
                     Jsonb({"icao": a["icao_code"] or None, "iata": a["iata_code"] or None, "city": a["municipality"] or None,
                            "size": "large" if big else "medium", "wikipedia": a["wikipedia_link"] or None})))
    _upsert_sites(conn, rows)
    conn.commit()
    return len(rows)


# ---- Railways ----

def load_rail(conn, client) -> int:
    if conn.execute("SELECT 1 FROM rail_lines LIMIT 1").fetchone():
        return 0
    import shapefile

    zf = zipfile.ZipFile(io.BytesIO(client.get(RAIL_ZIP).content))
    base = next(n[:-4] for n in zf.namelist() if n.endswith(".shp"))
    r = shapefile.Reader(shp=io.BytesIO(zf.read(base + ".shp")), dbf=io.BytesIO(zf.read(base + ".dbf")),
                         shx=io.BytesIO(zf.read(base + ".shx")))
    rows = []
    for sr in r.iterShapeRecords():
        pts, parts = sr.shape.points, list(sr.shape.parts) + [len(sr.shape.points)]
        for a, b in zip(parts, parts[1:]):
            line = pts[a:b]
            if len(line) >= 2:
                rows.append(("LINESTRING(" + ",".join(f"{x:.5f} {y:.5f}" for x, y in line) + ")", sr.record["continent"] or None))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO rail_lines (geom, continent) VALUES (ST_Transform(ST_GeomFromText(%s, 4326), 3857), %s)", rows)
    conn.commit()
    return len(rows)


# ---- US land borders ----

@lru_cache
def _us_towns() -> dict[str, list[tuple[float, float, int]]]:
    out: dict[str, list[tuple[float, float, int]]] = {}
    for f, pop_col in (("towns.tsv", "population"), ("cities.tsv", None)):
        with open(DATA / f, encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                if r["country"] == "US":
                    out.setdefault(r["name"].lower(), []).append((float(r["lat"]), float(r["lon"]), int(r.get(pop_col or "", 0) or 0)))
    return out


# Crossings in towns too small for the town list.
BORDER_TOWNS = {
    "alexandria bay": (44.3473, -75.9179), "blaine": (49.0021, -122.7566), "sumas": (49.0003, -122.2649), "lynden": (49.0024, -122.4853),
    "point roberts": (49.0024, -123.0680), "oroville": (48.9999, -119.4618), "sweetgrass": (48.9986, -111.9597),
    "pembina": (48.9990, -97.2393), "portal": (48.9972, -102.5494), "international falls": (48.6085, -93.4013),
    "sault ste. marie": (46.5100, -84.3601), "port huron": (42.9984, -82.4228), "champlain": (44.9937, -73.4492),
    "derby line": (45.0056, -72.0990), "highgate springs": (45.0137, -73.0852), "calais": (45.1885, -67.2786),
    "houlton": (46.1366, -67.7839), "madawaska": (47.3570, -68.3289), "jackman": (45.6243, -70.2558),
    "massena": (44.9906, -74.7400), "ogdensburg": (44.7330, -75.4610), "lewiston": (43.1490, -79.0444),
    "san ysidro": (32.5422, -117.0293), "otay mesa": (32.5497, -116.9383), "tecate": (32.5755, -116.6264),
    "calexico": (32.6790, -115.4989), "andrade": (32.7189, -114.7280), "san luis": (32.4870, -114.7822),
    "lukeville": (31.8803, -112.8166), "nogales": (31.3331, -110.9431), "naco": (31.3340, -109.9480),
    "douglas": (31.3341, -109.5604), "columbus": (31.7840, -107.6400), "santa teresa": (31.7840, -106.6800),
    "tornillo": (31.4440, -106.0820), "presidio": (29.5600, -104.3720), "del rio": (29.3270, -100.9280),
    "eagle pass": (28.7091, -100.4995), "rio grande city": (26.3790, -98.8200), "roma": (26.4053, -99.0150),
    "progreso": (26.0620, -97.9500), "pharr": (26.0870, -98.1856), "hidalgo": (26.0890, -98.2390),
    "los indios": (26.0480, -97.7450), "brownsville": (25.9017, -97.4975), "fabens": (31.4010, -106.1580),
    "boquillas": (29.1830, -102.9600), "sasabe": (31.4830, -111.5430), "norton": (45.0094, -71.7944),
    "b&m bridge": (25.8963, -97.5007), "b&m": (25.8963, -97.5007), "gateway": (25.8977, -97.4950),
    "veterans international": (25.8870, -97.4740), "bota": (31.7647, -106.4510), "bridge of the americas": (31.7647, -106.4510),
    "bridge of americas": (31.7647, -106.4510), "paso del norte": (31.7550, -106.4870), "ysleta": (31.6720, -106.3370),
    "stanton dcl": (31.7570, -106.4760), "anzalduas international bridge": (26.1370, -98.3300),
    "lewiston bridge": (43.1530, -79.0440), "peace bridge": (42.9070, -78.9050), "rainbow bridge": (43.0900, -79.0670),
    "whirlpool bridge": (43.1090, -79.0590),
}


def _names(n: str) -> list[str]:
    """Ways of writing a crossing's name: "Hidalgo/Pharr", "Otay Mesa Port of Entry", "Paso Del Norte (PDN)"."""
    import re

    n = re.sub(r"\((.*?)\)", r"/\1", (n or "").lower())
    n = re.sub(r"\b(port of entry|cargo facility|commercial|passenger)\b", "", n)
    out = []
    for part in [n] + n.split("/"):
        k = " ".join(part.split())
        if k:
            out += [k, k.removesuffix(" east").removesuffix(" west"), k.split(" ")[0]]
    return out


def place_crossing(port_name: str, crossing: str, border: str) -> tuple[float, float] | None:
    for n in (crossing, port_name):
        for cand in _names(n):
            if cand in BORDER_TOWNS:
                return BORDER_TOWNS[cand]
            towns = _us_towns().get(cand)
            if towns:
                # Of towns with this name, the one nearest the right border.
                near = (lambda t: -t[0]) if "Mexic" in border else (lambda t: t[0])
                lat, lon, _ = max(towns, key=near)
                if ("Mexic" in border and lat < 33.5) or ("Mexic" not in border and lat > 41):
                    return lat, lon
    return None


def _minutes(lane: dict | None) -> int | None:
    try:
        return int(lane["delay_minutes"]) if lane and lane.get("delay_minutes") not in ("", None) else None
    except (TypeError, ValueError):
        return None


def borders(conn, client) -> int:
    data = client.get(cfg().get("borders", {}).get("url") or "https://bwt.cbp.gov/api/bwtnew").json()
    rows, missing = [], []
    for b in data:
        where = place_crossing(b.get("port_name") or "", b.get("crossing_name") or "", b.get("border") or "")
        if where is None:
            missing.append(b.get("port_name"))
            continue
        cv = b.get("commercial_vehicle_lanes") or {}
        pv = b.get("passenger_vehicle_lanes") or {}
        name = b["port_name"] + (f" ({b['crossing_name']})" if b.get("crossing_name") and b["crossing_name"] != b["port_name"] else "")
        try:
            lanes = int(cv.get("maximum_lanes") or 0)
        except ValueError:
            lanes = 0
        stats = {"status": b.get("port_status"), "hours": b.get("hours"), "updated": f"{b.get('date')} {b.get('time')}",
                 "truck_lanes": lanes, "truck_delay_min": _minutes(cv.get("standard_lanes")),
                 "truck_fast_delay_min": _minutes(cv.get("FAST_lanes")),
                 "truck_lanes_open": (cv.get("standard_lanes") or {}).get("lanes_open") or None,
                 "truck_update": (cv.get("standard_lanes") or {}).get("update_time") or None,
                 "car_delay_min": _minutes(pv.get("standard_lanes"))}
        info = {"border": b.get("border"), "port_number": b.get("port_number"), "url": "https://bwt.cbp.gov/"}
        rows.append((f"cbp:{b['port_number']}", "border", name, "US", where[0], where[1], 1 + lanes / 5, Jsonb(info), Jsonb(stats)))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO logistics_sites (key, kind, name, country, lat, lon, rank, info, stats, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, lat = EXCLUDED.lat, lon = EXCLUDED.lon, rank = EXCLUDED.rank,
                 info = EXCLUDED.info, stats = EXCLUDED.stats, updated_at = now()""", rows)
    conn.commit()
    if missing:
        log.info("border waits: %d crossings not placed on the map: %s", len(missing), ", ".join(sorted(set(missing)))[:300])
    return len(rows)


PARTS = [("sites", "portwatch", load_sites), ("ports", "portwatch", port_stats), ("chokepoints", "portwatch", chokepoint_days),
         ("alerts", "portwatch", alerts), ("airports", None, load_airports), ("rail", None, load_rail), ("borders", "borders", borders)]


def run_network(conn: psycopg.Connection, client: httpx.Client | None = None) -> int:
    """Refresh whichever parts are due. One failing source does not hold up the others."""
    c = cfg()
    now = datetime.now(timezone.utc)
    client = client or _client()
    done = 0
    for part, section, fn in PARTS:
        if section and not (c.get(section) or {}).get("enabled", True):
            continue
        if not _due(conn, part, now):
            continue
        conn.commit()
        try:
            n = fn(conn, client)
            log.info("shipping %s: %d updated", part, n)
            done += n
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log.warning("shipping %s failed: %s", part, e)
            # Try again in an hour, not at the next step.
            kv_set(conn, f"shipping.{part}.at", (now - DUE[part] + timedelta(hours=1)).isoformat())
            conn.commit()
            continue
        _done(conn, part, now)
    return done
