"""Movement tracks: groups on the move, such as migrant caravans, followed from the news.

1. Spotting. When stories about a migrant caravan appear and no caravan track is active, a
   track is started. A track goes quiet after two weeks without news.
2. Reading. Every article about the caravan (headline and, when it has been read, full text)
   goes to the local model, which lists where the group was and when, how many people, and
   the sentence that says so. Destinations ("heading to Mexico City") are not positions.
3. Placing. Each place is looked up in GDELT's own tags for that article first (they include
   small towns), then places Wassup knows, then the city list. Only when all fail is the
   model's own guess of the coordinates used, if it falls inside the track's region; such
   points are marked approximate.
4. The path. tracks_api picks one position per day (the place most reports agree on) and
   leaves out a day that would mean a jump no caravan could make. You can confirm or reject
   any report in the tracks panel; rejected ones never come back.
"""
from __future__ import annotations

import csv
import logging
import math
import re
import time
import unicodedata
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ..geo import Place, gazetteer

log = logging.getLogger(__name__)

# Mexico, Central America and the US border.
CARAVAN_REGION = (-118.5, 6.5, -76.5, 33.6)
CARAVAN_WORDS = "caravan|caravana|caravane|karawane|carovana|karavan"
CARAVAN = re.compile(rf"\b({CARAVAN_WORDS})", re.I)
SQL_CARAVAN = rf"\m({CARAVAN_WORDS})"  # Postgres spells a word boundary \m, not \b
MIGRANT = re.compile(r"\b(migra|inmigra|immigra|asyl|asil|refugi|refugee|frontera|border|deport|éxodo|exodus|desplazad)", re.I)
PLOTTED = {"at", "arrived", "departed", "passed_through", "stopped"}
QUIET_DAYS = 14
PER_RUN = 30


def is_caravan_text(title: str, body: str | None = None) -> bool:
    """A migrant caravan, not a holiday caravan or a campaign caravan."""
    return bool(CARAVAN.search(title or "")) and bool(MIGRANT.search(f"{title} {(body or '')[:2500]}"))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


_city_index: dict[tuple[str, str], list] | None = None


def _cities() -> dict[tuple[str, str], list]:
    global _city_index
    if _city_index is None:
        idx: dict[tuple[str, str], list] = {}
        for c in gazetteer().cities:  # sorted by population, biggest first
            idx.setdefault((_norm(c.name), c.country), []).append(c)
        # Every town of 1,000 people or more along the caravan routes (scripts/build_towns.py).
        towns = Path(__file__).resolve().parent.parent / "data" / "towns.tsv"
        if towns.exists():
            with open(towns, encoding="utf-8") as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    p = Place(f"gn:{r['geonameid']}", r["name"], r["country"], "city", float(r["lat"]), float(r["lon"]))
                    for n in {_norm(r["name"]), _norm(r["asciiname"])}:
                        lst = idx.setdefault((n, r["country"]), [])
                        if all(q.key != p.key for q in lst):
                            lst.append(p)
        _city_index = idx
    return _city_index


def _pick(cands: list, near: tuple[float, float] | None):
    """Of several places with one name, the one nearest the group's last position (else the first)."""
    if not cands:
        return None
    if near is None:
        return cands[0]
    return min(cands, key=lambda c: km(near, (c["lat"], c["lon"]) if isinstance(c, dict) else (c.lat, c.lon)))


def _country_code(name: str | None) -> str | None:
    if not name:
        return None
    if len(name) == 2 and name.isalpha():
        return name.upper()
    p = gazetteer().by_name(name.strip())
    return p.country if p is not None and p.kind == "country" else None


def _in(region, lat: float, lon: float) -> bool:
    w, s, e, n = region
    return w <= lon <= e and s <= lat <= n


def geocode(conn: psycopg.Connection, place: str, country: str | None, item_id: int | None,
            region=CARAVAN_REGION, guess: tuple | None = None, near: tuple[float, float] | None = None
            ) -> tuple[float, float, str, str] | None:
    """(lat, lon, name, how) for a place named in an article, or None. Many towns share a name
    ("Álvaro Obregón" is a town in Chiapas and a borough of Mexico City), so when there are
    several, the one nearest `near` (the group's last known position) wins."""
    want = _norm(place)
    if not want:
        return None
    cc = _country_code(country)
    if item_id:
        for r in conn.execute(
                """SELECT p.name, p.lat, p.lon, p.country FROM item_places ip JOIN places p ON p.id = ip.place_id
                   WHERE ip.item_id = %s AND p.kind <> 'country'""", (item_id,)):
            if _norm(r["name"]) == want and _in(region, r["lat"], r["lon"]):
                return r["lat"], r["lon"], r["name"], "article"
    known = [r for r in conn.execute(
                 "SELECT name, lat, lon, country FROM places WHERE lower(name) = lower(%s) AND kind <> 'country' LIMIT 20",
                 (place.strip(),))
             if _in(region, r["lat"], r["lon"]) and (cc is None or r["country"] == cc)]
    r = _pick(known, near)
    if r is not None:
        return r["lat"], r["lon"], r["name"], "known"
    idx = _cities()
    codes = [cc] if cc else ["MX", "GT", "HN", "SV", "NI", "CR", "PA", "US", "BZ", "CO", "VE"]
    c = _pick([c for code in codes for c in idx.get((want, code), []) if _in(region, c.lat, c.lon)], near)
    if c is not None:
        return c.lat, c.lon, c.name, "gazetteer"
    if guess and guess[0] is not None and guess[1] is not None and near is not None:
        # The model's own estimate, only when it is plausible: within a day's travel or so of
        # the last known position (a park or a shelter is often named instead of a town).
        lat, lon = float(guess[0]), float(guess[1])
        if _in(region, lat, lon) and km(near, (lat, lon)) <= 100:
            return lat, lon, place.strip(), "approximate"
    return None


SCHEMA = {
    "type": "object",
    "properties": {
        "about_group": {"type": "boolean"},
        "reports": {"type": "array", "maxItems": 6, "items": {
            "type": "object",
            "properties": {
                "place": {"type": "string"}, "country": {"type": "string"},
                "date": {"type": ["string", "null"]},
                "status": {"type": "string", "enum": ["at", "arrived", "departed", "passed_through", "stopped", "heading_to"]},
                "people": {"type": ["integer", "null"]},
                "quote": {"type": "string"},
                "lat": {"type": ["number", "null"]}, "lon": {"type": ["number", "null"]},
            },
            "required": ["place", "country", "date", "status", "people", "quote", "lat", "lon"]}},
    },
    "required": ["about_group", "reports"],
}


def extract_positions(llm, title: str, published: datetime, text: str | None) -> dict:
    body = " ".join((text or "").split())[:5000]
    # Models are poor at working out "last Sunday" or "on Tuesday"; a calendar removes the arithmetic.
    calendar = "; ".join(f"{d:%A} = {d:%Y-%m-%d}" for d in (published.date() - timedelta(days=i) for i in range(13, -1, -1)))
    prompt = f"""You track a migrant caravan (a large group of migrants travelling together) for a news analyst.
Article published {published:%A %B %-d, %Y}. The two weeks up to then: {calendar}.
Headline: {title}
{('Article text: ' + body) if body else '(Only the headline is available.)'}

about_group: true only if the article reports on a migrant caravan or a large migrant group on the move.
reports: every place the article says the group (or part of it) physically was, with:
- place: the town or city, as named in the article (not a region or country unless nothing finer is given)
- country: the country it is in
- date: the date the group was there, as YYYY-MM-DD. Use the calendar above for words like "last Sunday",
  "on Tuesday", "yesterday" or "this morning" (a weekday with no other date means the most recent one up to
  the publication date). null if the article does not say when; do not guess.
- status: at (was there), arrived, departed (left this place), passed_through, stopped (stopped or held by
  authorities), or heading_to (a destination: "set off for X", "rumbo a X", "hacia X" are heading_to X)
- people: the number of people the article gives for the group at that point, or null
- quote: the sentence from the article that says so, copied exactly
- lat, lon: your best estimate of the place's coordinates, or null if you do not know it
Only report what the article says. Do not use places mentioned for other reasons (where officials spoke,
where a report was published). Return no reports if the article gives no positions."""
    return llm(prompt, SCHEMA)


def _parse_date(s: str | None, published: datetime, read_at: datetime | None = None) -> tuple[datetime, bool]:
    """When the group was there; (published date, False) when the model gave none or nonsense.
    Pages are often updated after they are first published, so a date up to the day the page
    was read is accepted."""
    if s:
        try:
            d = date.fromisoformat(s[:10])
            latest = max(published, read_at or published).date() + timedelta(days=1)
            if published.date() - timedelta(days=45) <= d <= latest:
                return datetime.combine(d, dtime(12), tzinfo=timezone.utc), True
        except ValueError:
            pass
    return published, False


def ensure_caravan_track(conn: psycopg.Connection) -> int | None:
    """The active caravan track, started when caravan stories appear; None when there is none."""
    t = conn.execute("SELECT id, meta FROM tracks WHERE kind = 'movement' AND active AND meta->>'kind' = 'caravan' ORDER BY id DESC LIMIT 1").fetchone()
    recent = conn.execute(
        """SELECT s.id, coalesce(s.title_en, s.title) AS title, s.first_seen FROM stories s
           WHERE s.last_seen > now() - interval '7 days' AND s.item_count >= 2
             AND coalesce(s.title_en, s.title) ~* %s ORDER BY s.first_seen""",
        (SQL_CARAVAN,)).fetchall()
    recent = [r for r in recent if is_caravan_text(r["title"], r["title"])]
    if t:
        last = conn.execute("SELECT max(observed_at) AS t FROM track_observations WHERE track_id = %s", (t["id"],)).fetchone()["t"]
        if not recent and (last is None or last < datetime.now(timezone.utc) - timedelta(days=QUIET_DAYS)):
            conn.execute("UPDATE tracks SET active = false WHERE id = %s", (t["id"],))
            log.info("caravan track %s is quiet; closed", t["id"])
            return None
        return t["id"]
    if not recent:
        return None
    since = recent[0]["first_seen"] - timedelta(days=3)
    name = f"Migrant caravan, from {recent[0]['first_seen']:%b %-d}"
    row = conn.execute(
        """INSERT INTO tracks (key, name, kind, desk, source, description, story_id, meta)
           VALUES (%s, %s, 'movement', 'migration', 'News reports',
                   'Where the caravan has been, from news reports read by the local model.', %s, %s) RETURNING id""",
        (f"caravan-{since:%Y%m%d}", name, recent[0]["id"],
         Jsonb({"kind": "caravan", "region": CARAVAN_REGION, "since": since.isoformat()}))).fetchone()
    log.info("started a caravan track: %s", name)
    return row["id"]


def run_movements(conn: psycopg.Connection, llm=None, max_seconds: float = 240) -> int:
    track = ensure_caravan_track(conn)
    conn.commit()
    if track is None:
        return 0
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    meta = conn.execute("SELECT meta FROM tracks WHERE id = %s", (track,)).fetchone()["meta"]
    region = tuple(meta.get("region") or CARAVAN_REGION)
    since = datetime.fromisoformat(meta["since"])
    rows = conn.execute(
        """SELECT i.id, coalesce(i.title_en, i.title) AS title, i.url, i.published_at, t.body, t.fetched_at AS read_at
           FROM items i LEFT JOIN item_texts t ON t.item_id = i.id AND t.status = 'ok'
           WHERE i.published_at >= %s AND coalesce(i.title_en, i.title) ~* %s
             AND NOT EXISTS (SELECT 1 FROM track_reads r WHERE r.track_id = %s AND r.item_id = i.id)
           ORDER BY (t.body IS NOT NULL) DESC, i.published_at DESC LIMIT %s""",
        (since, SQL_CARAVAN, track, PER_RUN * 3)).fetchall()
    conn.commit()
    started, done = time.time(), 0
    for it in rows:
        if time.time() - started > max_seconds or done >= PER_RUN:
            break
        found = 0
        if is_caravan_text(it["title"], it["body"]):
            try:
                out = extract_positions(llm, it["title"], it["published_at"], it["body"])
            except Exception as e:
                log.warning("caravan extraction failed for item %s: %s", it["id"], e)
                continue  # not marked read: try again next time
            if out.get("about_group"):
                found = _store_reports(conn, track, it, out.get("reports") or [], region)
        conn.execute("INSERT INTO track_reads (track_id, item_id, positions) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                     (track, it["id"], found))
        conn.commit()
        done += 1
    if done:
        log.info("caravan: read %d articles", done)
    return done


def _quote_checks(place: str, quote: str, title: str, body: str | None) -> bool:
    """The model must quote the article, and the quote must name the place. Small models
    sometimes pair a place with a sentence about another one, or paraphrase."""
    q, src, pl = _norm(quote), _norm(f"{title} {body or ''}"), _norm(place)
    if len(q) < 12:
        return False
    head = q[:60]
    if head not in src and q[-40:] not in src:
        return False
    words = [w for w in pl.split() if len(w) >= 4] or pl.split()
    return any(w in q for w in words)


def _store_reports(conn, track: int, it: dict, reports: list[dict], region) -> int:
    n = 0
    last = conn.execute(
        """SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM track_observations
           WHERE track_id = %s AND category = 'position' AND status <> 'rejected' AND observed_at <= %s
           ORDER BY (status = 'confirmed') DESC, observed_at DESC LIMIT 1""", (track, it["published_at"])).fetchone()
    near = (last["lat"], last["lon"]) if last else None
    for r in reports[:6]:
        if r.get("status") not in PLOTTED or not (r.get("place") or "").strip():
            continue
        if not _quote_checks(r["place"], r.get("quote") or "", it["title"], it.get("body")):
            continue
        where = geocode(conn, r["place"], r.get("country"), it["id"], region, (r.get("lat"), r.get("lon")), near)
        if where is None:
            continue
        lat, lon, name, how = where
        near = near or (lat, lon)  # a new track: the article's first place guides the rest
        when, dated = _parse_date(r.get("date"), it["published_at"], it.get("read_at"))
        people = r.get("people") if isinstance(r.get("people"), int) and 0 < r["people"] < 1_000_000 else None
        confidence = (0.8 if dated else 0.35) * (0.6 if how == "approximate" else 1.0)
        conn.execute(
            """INSERT INTO track_observations (track_id, observed_at, category, geom, label, props, source_url, item_id, confidence)
               VALUES (%s, %s, 'position', ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s, %s)""",
            (track, when, lon, lat, name,
             Jsonb({"status": r["status"], "people": people, "quote": (r.get("quote") or "")[:400], "placed_by": how,
                    "dated": dated, "headline": it["title"][:200]}),
             it["url"], it["id"], round(confidence, 2)))
        n += 1
    return n


# --- the path ------------------------------------------------------------------------------

MAX_KM_PER_DAY = 250  # on foot a caravan covers 20 to 40 km a day; buses and trains far more, but not this
AGREE_KM = 40         # reports this close on the same day agree (a day's walk)
STATUS_WEIGHT = {"arrived": 1.0, "at": 1.0, "stopped": 1.0, "passed_through": 0.8, "departed": 0.6}


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return 6371 * math.acos(max(-1.0, min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl))))


def daily_path(points: list[dict]) -> tuple[list[dict], set[int]]:
    """One position per day and the ids of reports that disagree with the path.

    points: dicts with id, day (date), lat, lon, status ('confirmed' wins), confidence.
    For each day the place with the most support wins (reports within AGREE_KM count together,
    weighted by confidence and by how reliable their status is);
    then any day that would need more than MAX_KM_PER_DAY from the previous day is left out.
    """
    def weight(p):
        # "departed" is the label models most often get wrong ("set off for X"), so it counts less.
        return (p["confidence"] or 0.5) * STATUS_WEIGHT.get(p.get("report_status") or "at", 1.0)

    by_day: dict[date, list[dict]] = {}
    for p in points:
        by_day.setdefault(p["day"], []).append(p)
    picks = []
    for day in sorted(by_day):
        ps = by_day[day]
        confirmed = [p for p in ps if p["status"] == "confirmed"]
        pool = confirmed or ps
        def support(p):
            return sum(weight(q) for q in pool if km((p["lat"], p["lon"]), (q["lat"], q["lon"])) <= AGREE_KM)
        best = max(pool, key=lambda p: (support(p), weight(p)))
        picks.append({**best, "confirmed_day": bool(confirmed)})
    path, prev = [], None
    for p in picks:
        if prev is not None and not p["confirmed_day"]:
            days = max(1, (p["day"] - prev["day"]).days)
            if km((prev["lat"], prev["lon"]), (p["lat"], p["lon"])) / days > MAX_KM_PER_DAY:
                continue
        path.append(p)
        prev = p
    on_path = {p["id"] for p in path}
    near = set()
    for p in points:
        day_pick = next((q for q in path if q["day"] == p["day"]), None)
        if day_pick and km((p["lat"], p["lon"]), (day_pick["lat"], day_pick["lon"])) <= AGREE_KM:
            near.add(p["id"])
    outliers = {p["id"] for p in points if p["id"] not in on_path and p["id"] not in near and p["status"] != "confirmed"}
    return path, outliers
