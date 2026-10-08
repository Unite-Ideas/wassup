"""Strikes on the map: missile, drone, air and artillery strikes reported on Telegram.

1. Picking. New Telegram posts (from the last 36 hours, newest first) that mention a strike,
   an explosion, a drone or a missile in any of the languages the channels write in.
2. Reading. The local model lists every strike the post reports as having happened: where
   (village, town or city, and its province), when, with what, by whom, what was hit, the
   outcome (hit, intercepted, claimed, explosions heard) and the sentence that says so.
   Threats, alerts and old events being remembered are not strikes.
3. Placing. The place is looked up in a list of 76,000 places (every village in Ukraine,
   Lebanon, Syria, Israel and Palestine and the Russian border regions, and towns elsewhere,
   in their Latin, Cyrillic, Arabic and Hebrew spellings; scripts/build_conflict_places.py).
   When several places share the name, the province decides; when it cannot, the report is
   left off the map rather than put in the wrong place. The model never guesses coordinates.
4. Grouping. Many channels report the same strike. tracks_api groups reports of one place
   within six hours into one strike, and counts how many channels (and from which side) and
   how many news outlets reported it. One channel alone is shown as unconfirmed.
"""
from __future__ import annotations

import csv
import gzip
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ..geo import gazetteer
from .movement import km

log = logging.getLogger(__name__)

REGIONS = {
    "ukraine": {"name": "Strikes: Ukraine and Russia", "desk": "russia_ukraine",
                "bbox": (19.0, 40.0, 180.0, 78.0), "countries": {"UA", "RU", "BY", "MD"}},  # deep strikes reach Siberia
    "mideast": {"name": "Strikes: Middle East", "desk": "iran_mideast",
                "bbox": (32.0, 11.5, 63.5, 39.0), "countries": {"IL", "PS", "LB", "SY", "IQ", "IR", "YE", "JO"}},
}
WINDOW_HOURS = 36      # posts older than this when first seen are not read (a backlog after a restart)
PER_RUN = 60
GROUP_KM = 6           # reports this close ...
GROUP_HOURS = 6        # ... and this close in time are one strike
WEAPONS = ["missile", "drone", "glide_bomb", "airstrike", "artillery", "rocket", "unknown"]
OUTCOMES = ["hit", "intercepted", "claimed", "explosions_heard"]

# A post worth asking about. Wide on purpose: the model decides; this only skips the posts that
# cannot be about a strike (speeches, politics, prices), which are most of them.
STRIKE = re.compile(
    r"strike|struck|explosi|blast|drone|missile|shell|bomb|attack|\bhit\b|intercept|shahed|geran|rocket|artiller|"
    r"kamikaze|uav|ballistic|air defen|air raid|"
    r"удар|вибух|взрыв|обстр|ракет|дрон|бпла|шахед|герань|прил[еіи]т|атак|артил|бомб|ппо|пво|збит|сбит|влуч|попадан|"
    r"уражен|поражен|каб\b|fpv|"
    r"غار[ةا]|قصف|انفجار|صاروخ|صواريخ|مسير|استهداف|اعتراض|"
    r"פיצוץ|טיל|רקט|כטב|תקיפ|יירוט|"
    r"حمله|موشک|پهپاد", re.I)
# A drone or missile on its way somewhere ("БпЛА повз Жашків курсом на Вінниччину") is not a
# strike, and small models often say it is. A quote with flight words and nothing about an impact
# is dropped whatever the model says.
FLIGHT = re.compile(r"курс(ом)? на|повз|у напрямку|в направлении|в сторону|в бік|рухає|heading|towards|toward|on course|"
                    r"en route|flying over|in flight", re.I)
IMPACT = re.compile(r"влуч|попад|уражен|поражен|вибух|взрыв|explo|\bhit|struck|strike|damag|пошкодж|поврежд|пожеж|пожар|"
                    r"\bfire|загин|погиб|поранен|ранен|killed|injur|збит|сбит|знищ|уничтож|intercept|shot down|destroy|"
                    r"انفجار|غارة|قصف|استهداف|פיצוץ|נפילה|פגיעה", re.I)
# OSINT channels often give the exact spot: "Coordinates: 50.4533461, 30.4356499".
COORDS = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{3,})\s*[,;]\s*(-?\d{1,3}\.\d{3,})(?![\d.])")
# "Рязанщина", "Kharkiv region": the post names a province, not a town. The strike is then put
# on the province's main city and marked as only roughly placed.
PROVINCE_ONLY = re.compile(r"щин[аиіуо]|\b(oblast|region|province|governorate|krai|област|облас|край|محافظة)", re.I)
_PLACE_WORDS = {"village", "town", "city", "of", "the", "settlement", "district", "село", "селище", "місто", "город",
                "поселок", "посёлок", "смт", "пгт", "м", "с", "г", "п", "selo", "smt", "al",
                "region", "oblast", "province", "governorate", "krai", "область", "области", "обл"}


def unorm(s: str | None) -> str:
    """Lower case, no accents or apostrophes, any script: 'Pokrovs'k' -> 'pokrovsk', 'Ёлка' -> 'елка'."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"['’`ʼʼ]", "", s)
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


class Places:
    """The conflict place list, by every spelling of every name."""

    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).resolve().parent.parent / "data" / "conflict_places.tsv.gz"
        self.by_name: dict[str, list[tuple]] = {}
        if not path.exists():
            log.warning("no conflict place list at %s: strikes are placed with big cities only", path)
            return
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\"):
                p = (r["geonameid"], r["name"], r["country"], r["admin1"], int(r["population"] or 0),
                     float(r["lat"]), float(r["lon"]))
                for n in {unorm(r["name"]), *(unorm(x) for x in r["names"].split("|") if x)}:
                    if n:
                        self.by_name.setdefault(n, []).append(p)

    def lookup(self, name: str) -> list[tuple]:
        n = unorm(name)
        found = self.by_name.get(n)
        if found is None:  # "the village of X", "с. X"
            words = [w for w in n.split() if w not in _PLACE_WORDS]
            found = self.by_name.get(" ".join(words), [])
        return found


_places: Places | None = None


def places() -> Places:
    global _places
    if _places is None:
        _places = Places()
    return _places


def _same_admin(region: str, admin1: str) -> bool:
    """'Donetsk Oblast', 'Donetsk region' and 'Donetsk' are one province; so are 'Zaporizhzhia'
    and 'Zaporizhia'. The first five letters of the first word decide."""
    a, b = unorm(region).split(), unorm(admin1).split()
    return bool(a and b) and a[0][:5] == b[0][:5]


def _in(bbox, lat: float, lon: float) -> bool:
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def region_of(country: str, lat: float, lon: float) -> str | None:
    for key, r in REGIONS.items():
        if country in r["countries"] and _in(r["bbox"], lat, lon):
            return key
    return None


def geocode(place: str, original: str | None, province: str | None, country: str | None) -> dict | None:
    """Where a reported place is, or None when it is unknown or cannot be told apart from
    another place of the same name."""
    cands = {c[0]: c for n in (place, original) if n for c in places().lookup(n)}
    allowed = set().union(*(r["countries"] for r in REGIONS.values()))
    cs = [c for c in cands.values() if c[2] in allowed]
    cc = (country or "").strip()
    cc = cc.upper() if len(cc) == 2 else (_country_code(cc) or "")
    if cc and any(c[2] == cc for c in cs):
        cs = [c for c in cs if c[2] == cc]
    if province and any(_same_admin(province, c[3]) for c in cs):
        cs = [c for c in cs if _same_admin(province, c[3])]
    if not cs:
        p = gazetteer().by_name((place or "").strip())  # a big city spelt in a way the list lacks
        if p is not None and p.kind == "city" and p.country in allowed:
            return {"lat": p.lat, "lon": p.lon, "name": p.name, "country": p.country, "admin1": "", "how": "gazetteer"}
        return None
    cs.sort(key=lambda c: -c[4])
    best = cs[0]
    if len(cs) > 1 and any(km((best[5], best[6]), (c[5], c[6])) > 25 for c in cs[1:]):
        # Same name, different places. A city wins over villages that share its name; otherwise
        # the report cannot be placed.
        if not (best[4] >= 20000 and best[4] >= 10 * cs[1][4]):
            return None
    return {"lat": best[5], "lon": best[6], "name": best[1], "country": best[2], "admin1": best[3], "how": "list"}


def _country_code(name: str) -> str | None:
    p = gazetteer().by_name(name.strip()) if name else None
    return p.country if p is not None and p.kind == "country" else None


SCHEMA = {
    "type": "object",
    "properties": {
        "strikes": {"type": "array", "maxItems": 8, "items": {
            "type": "object",
            "properties": {
                "place": {"type": "string"}, "place_original": {"type": "string"},
                "province": {"type": "string"}, "country": {"type": "string"},
                "date": {"type": ["string", "null"]},
                "weapon": {"type": "string", "enum": WEAPONS},
                "attacker": {"type": "string"},
                "outcome": {"type": "string", "enum": OUTCOMES},
                "target": {"type": "string"},
                "killed": {"type": ["integer", "null"]}, "injured": {"type": ["integer", "null"]},
                "quote": {"type": "string"},
            },
            "required": ["place", "place_original", "province", "country", "date", "weapon", "attacker", "outcome",
                         "target", "killed", "injured", "quote"]}},
    },
    "required": ["strikes"],
}


def extract_strikes(llm, text: str, published: datetime, channel: str, lean: str | None) -> dict:
    calendar = "; ".join(f"{d:%A} = {d:%Y-%m-%d}" for d in (published.date() - timedelta(days=i) for i in range(3, -1, -1)))
    prompt = f"""You map strikes for a news analyst. Below is a Telegram post from the channel {channel}{f' ({lean} side)' if lean else ''},
posted {published:%A %B %-d, %Y at %H:%M} UTC. Recent days: {calendar}.

Post:
{text[:3500]}

strikes: every strike or attack the post reports as having happened, one per place: missile, drone, glide bomb,
air strike, rocket or artillery fire, or explosions. Not: drones or missiles reported in flight or heading somewhere
("UAV past X towards Y", "missile course on Z"), threats, warnings, air raid alerts on their own, forecasts, ground
fighting between troops, or strikes from months or years ago being remembered. A post about the aftermath of an earlier
strike (officials visiting the site, funerals, rescue work, a death toll updated days later) reports no new strike:
include it only if the post says on which day it happened, with that date. For each:
- place: the village, town or city, in English spelling (Ukrainian spelling for places in Ukraine), nominative
  form, without words like "village of". Not a province or country unless the post names nothing finer.
- place_original: the same place exactly as the post writes it, in the nominative (dictionary) form
- province: its province, oblast or governorate in English (for example Kharkiv Oblast), or "" if not given
- country: the country the place is in
- date: YYYY-MM-DD if the post says the strike was on an earlier day (use the days above for "yesterday" or
  "overnight on Tuesday"); null if it happened today or the post does not say
- weapon: missile, drone, glide_bomb, airstrike, artillery, rocket, or unknown
- attacker: who carried it out, if the post says or it is plain (Russia, Ukraine, Israel, Iran, Hezbollah, Hamas,
  Houthis, United States), else unknown
- outcome: hit (something was hit), intercepted (shot down, no hit), claimed (a side claims a hit it does not
  show), or explosions_heard (only explosions reported)
- target: what was hit, in a few words (energy facility, apartment block, oil depot), or ""
- killed, injured: numbers the post gives for this strike, or null
- quote: the sentence from the post that reports it, copied exactly, in the post's language
Return an empty list if the post reports no strikes."""
    return llm(prompt, SCHEMA)


def _quote_ok(quote: str, post: str, names: list[str]) -> bool:
    """The model must quote the post, the quote must name the place (allowing for case endings:
    'Харкові' names 'Харків'), and it must not be only a flight path."""
    if FLIGHT.search(quote or "") and not IMPACT.search(quote or ""):
        return False
    q, src = unorm(quote), unorm(post)
    if len(q) < 10 or (q[:50] not in src and q[-35:] not in src):
        return False
    for n in names:
        for w in unorm(n).split():
            if len(w) >= 4 and w[:max(4, len(w) - 2)] in q:
                return True
    return False


def exact_spot(text: str, near: tuple[float, float], max_km: float = 30) -> tuple[float, float] | None:
    """Coordinates given in the post, if they are close to the named place (so they belong to it)."""
    for m in COORDS.finditer(text or ""):
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and km(near, (lat, lon)) <= max_km:
            return lat, lon
    return None


def _parse_date(s: str | None, published: datetime) -> tuple[datetime, bool]:
    if s:
        try:
            d = date.fromisoformat(s[:10])
            if published.date() - timedelta(days=3) <= d < published.date():
                return datetime.combine(d, dtime(12), tzinfo=timezone.utc), True
        except ValueError:
            pass
    return published, False


def ensure_tracks(conn: psycopg.Connection) -> dict[str, int]:
    ids = {}
    for key, r in REGIONS.items():
        row = conn.execute(
            """INSERT INTO tracks (key, name, kind, desk, source, description, meta)
               VALUES (%s, %s, 'strikes', %s, 'Telegram, read by the local model',
                       'Strikes reported on Telegram, grouped when several channels report the same one.', %s)
               ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name RETURNING id""",
            (f"strikes-{key}", r["name"], r["desk"], Jsonb({"region": key, "bbox": r["bbox"]}))).fetchone()
        ids[key] = row["id"]
    conn.commit()
    return ids


def run_strikes(conn: psycopg.Connection, llm=None, max_seconds: float = 240) -> int:
    if os.environ.get("STRIKES", "on") == "off":
        return 0
    tracks = ensure_tracks(conn)
    rows = conn.execute(
        """SELECT i.id, i.url, i.published_at, i.meta, coalesce(t.body, i.summary, i.title) AS text, i.outlet
           FROM items i JOIN sources s ON s.id = i.source_id AND s.kind = 'telegram'
           LEFT JOIN item_texts t ON t.item_id = i.id
           WHERE i.published_at > now() - %s * interval '1 hour'
             AND NOT EXISTS (SELECT 1 FROM strike_reads r WHERE r.item_id = i.id)
           ORDER BY i.published_at DESC LIMIT %s""", (WINDOW_HOURS, PER_RUN * 4)).fetchall()
    conn.commit()
    if not rows:
        return 0
    skip = [r["id"] for r in rows if len(r["text"] or "") < 30 or not STRIKE.search(r["text"])]
    if skip:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO strike_reads (item_id, strikes) VALUES (%s, 0) ON CONFLICT DO NOTHING", [(i,) for i in skip])
        conn.commit()
    skipped = set(skip)
    todo = [r for r in rows if r["id"] not in skipped][:PER_RUN]
    if not todo:
        return 0
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    parallel = max(1, int(os.environ.get("STRIKES_PARALLEL", "2")))
    started, done, found = time.time(), 0, 0

    def ask(it):
        m = it["meta"] or {}
        try:
            return it, extract_strikes(llm, it["text"], it["published_at"], f"@{m.get('handle')} ({it['outlet']})", m.get("lean"))
        except Exception as e:
            log.warning("strike extraction failed for item %s: %s", it["id"], e)
            return it, None

    with ThreadPoolExecutor(parallel) as ex:
        for i in range(0, len(todo), parallel * 2):
            if time.time() - started > max_seconds:
                break
            for it, out in ex.map(ask, todo[i:i + parallel * 2]):
                if out is None:
                    continue  # not marked read: tried again next time
                n = _store(conn, tracks, it, out.get("strikes") or [])
                conn.execute("INSERT INTO strike_reads (item_id, strikes) VALUES (%s, %s) ON CONFLICT DO NOTHING", (it["id"], n))
                conn.commit()
                done += 1
                found += n
    if done:
        log.info("strikes: read %d posts, %d strikes placed", done, found)
    return done


def _store(conn, tracks: dict[str, int], it: dict, strikes: list[dict]) -> int:
    m = it["meta"] or {}
    n, seen = 0, set()
    for s in strikes[:8]:
        if not (s.get("place") or "").strip() or s.get("weapon") not in WEAPONS or s.get("outcome") not in OUTCOMES:
            continue
        if not _quote_ok(s.get("quote") or "", it["text"], [s["place"], s.get("place_original") or ""]):
            continue
        where = geocode(s["place"], s.get("place_original"), s.get("province"), s.get("country"))
        if where is None:
            continue
        spot = exact_spot(it["text"], (where["lat"], where["lon"]))
        if spot:
            where = {**where, "lat": spot[0], "lon": spot[1], "how": "coordinates"}
        elif PROVINCE_ONLY.search(f"{s['place']} {s.get('place_original') or ''}"):
            where = {**where, "how": "province"}
        region = region_of(where["country"], where["lat"], where["lon"])
        if region is None or (where["name"], region) in seen:
            continue
        seen.add((where["name"], region))
        when, dated = _parse_date(s.get("date"), it["published_at"])
        attacker = (s.get("attacker") or "").strip()
        props = {"place": s["place"], "admin1": where["admin1"], "weapon": s["weapon"], "outcome": s["outcome"],
                 "attacker": "" if attacker.lower() == "unknown" else attacker[:40], "target": (s.get("target") or "")[:80],
                 "killed": s.get("killed"), "injured": s.get("injured"), "quote": s["quote"][:400],
                 "handle": m.get("handle"), "channel": it["outlet"], "lean": m.get("lean"), "dated": dated,
                 "placed_by": where["how"], "geoname": where["name"]}
        conn.execute(
            """INSERT INTO track_observations (track_id, observed_at, category, geom, label, props, source_url, item_id, confidence)
               VALUES (%s, %s, 'strike', ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s, %s)""",
            (tracks[region], when, where["lon"], where["lat"], s["place"].strip()[:80], Jsonb(props), it["url"], it["id"],
             {"gazetteer": 0.7, "province": 0.4}.get(where["how"], 0.9)))
        n += 1
    return n


# --- grouping --------------------------------------------------------------------------------

def group_strikes(reports: list[dict], at: datetime) -> list[dict]:
    """One strike from several reports of the same place within GROUP_HOURS.

    reports: dicts with id, observed_at, lat, lon, label, status, props, source_url, news (how
    many independent news outlets carry the story the post joined). Oldest first is not needed.
    """
    events: list[dict] = []
    for r in sorted(reports, key=lambda r: r["observed_at"]):
        ev = next((e for e in reversed(events)
                   if r["observed_at"] - e["last"] <= timedelta(hours=GROUP_HOURS)
                   and km((e["lat"], e["lon"]), (r["lat"], r["lon"])) <= GROUP_KM), None)
        if ev is None:
            ev = {"lat": r["lat"], "lon": r["lon"], "first": r["observed_at"], "last": r["observed_at"], "reports": []}
            events.append(ev)
        ev["last"] = r["observed_at"]
        ev["reports"].append(r)
    out = []
    for e in events:
        rs = e["reports"]
        p = [r["props"] or {} for r in rs]

        def common(key, skip=("", "unknown", None)):
            c = Counter(x.get(key) for x in p if x.get(key) not in skip)
            return c.most_common(1)[0][0] if c else None

        outcomes = {x.get("outcome") for x in p}
        channels = sorted({x.get("handle") for x in p if x.get("handle")})
        sides = sorted({x.get("lean") for x in p if x.get("lean")})
        news = max((r.get("news") or 0) for r in rs)
        confirmed = any(r["status"] == "confirmed" for r in rs)
        first = rs[0]
        out.append({
            "ids": [r["id"] for r in rs], "lat": e["lat"], "lon": e["lon"], "label": first["label"],
            "admin1": p[0].get("admin1"), "first": e["first"].isoformat(), "last": e["last"].isoformat(),
            "age_hours": round((at - e["first"]).total_seconds() / 3600, 1),
            "weapon": common("weapon") or "unknown", "attacker": common("attacker") or "",
            "outcome": "hit" if "hit" in outcomes else common("outcome") or "explosions_heard",
            "target": common("target") or "",
            "killed": max((x.get("killed") or 0) for x in p) or None, "injured": max((x.get("injured") or 0) for x in p) or None,
            "reports": len(rs), "channels": len(channels), "sides": sides, "news": news, "confirmed": confirmed,
            "corroborated": confirmed or len(channels) >= 2 or news >= 2,
            "quote": p[0].get("quote"),
            "rough": all(x.get("placed_by") == "province" for x in p),  # only the province is known
            "sources": [{"handle": x.get("handle"), "channel": x.get("channel"), "lean": x.get("lean"),
                         "url": r["source_url"], "at": r["observed_at"].isoformat(), "quote": x.get("quote")}
                        for r, x in list(zip(rs, p))[:6]],
        })
    return out
