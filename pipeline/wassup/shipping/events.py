"""Disruptions to trade and transport, read from the Shipping desk's stories (and other desks'
stories with freight in the headline).

Each story on the desk is shown to the local model, a batch at a time, with its headlines. It
says whether the story reports a disruption (a port closed, a strike at the docks, a ship
attacked or seized, a canal or strait blocked, a border shut to trucks, a rail line cut, a new
tariff or export ban), what kind, where, how serious, and whether it is still going on. The
place is looked up among the ports, chokepoints, airports and border crossings first, then the
town lists, then the country; if none fits, the story's own dot is used.

A story is read again when it has doubled in size, so an event's status follows the news.
"""
from __future__ import annotations

import logging
import re
import time

import psycopg
from psycopg.types.json import Jsonb

from ..geo import gazetteer

log = logging.getLogger(__name__)

BATCH = 8
PER_RUN = 40
KINDS = {
    "port_closure": "Port closed or restricted", "labour_strike": "Strike or labour action", "attack_on_ship": "Attack on a ship",
    "seizure": "Ship or cargo seized", "chokepoint": "Canal or strait disrupted", "congestion": "Congestion or delays",
    "accident": "Accident or spill", "border": "Border crossing closed or slowed", "rail": "Rail freight disrupted",
    "trucking": "Trucking disrupted", "air_cargo": "Air cargo disrupted", "tariff": "Tariff or trade measure",
    "sanctions": "Sanctions, embargo or export ban", "weather": "Storm or natural hazard", "other": "Other disruption",
}

# Freight words in a headline, for stories another desk took: a tanker struck in the Strait of
# Hormuz is on the Iran desk, and still a shipping disruption. The model decides if it is one.
FREIGHT = (r"\m(tankers?|cargo ships?|container ships?|containerships?|freighters?|bulk carriers?|merchant (ships?|vessels?)|"
           r"vessels?|shipping|seafarers|ports?|seaports?|harbou?r|dockworkers|longshoremen|strait of hormuz|hormuz|bab el-mandeb|"
           r"red sea|suez|panama canal|black sea grain|malacca|freight|cargo|rail(way|road)s?|truckers|trucking|border crossings?|"
           r"tariffs?|export bans?|import bans?|embargo|blockade|customs|supply chains?)\M")

PENDING = """
    SELECT s.id, coalesce(s.title_en, s.title) AS title, s.item_count, s.first_seen, s.last_seen,
           p.name AS place_name, p.country AS place_country, p.lat AS place_lat, p.lon AS place_lon
    FROM stories s LEFT JOIN places p ON p.id = s.primary_place_id
    WHERE s.routed AND s.last_seen > now() - interval '10 days'
      AND (s.desk = 'shipping' OR coalesce(s.title_en, s.title) ~* %s)
      AND NOT EXISTS (SELECT 1 FROM shipping_events e WHERE e.key = 'story:' || s.id AND s.item_count < 2 * coalesce(e.items, 0))
    ORDER BY s.desk = 'shipping' DESC, s.last_seen > now() - interval '1 day' DESC, s.significance DESC, s.item_count DESC LIMIT %s"""


def schema() -> dict:
    return {"type": "object", "properties": {"stories": {"type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "integer"},
        "disruption": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "place": {"type": "string"},
        "country": {"type": "string"},
        "severity": {"type": "integer", "enum": [1, 2, 3]},
        "status": {"type": "string", "enum": ["ongoing", "ended", "threatened"]},
        "summary": {"type": "string"}},
        "required": ["n", "disruption", "kind", "place", "country", "severity", "status", "summary"]}}},
        "required": ["stories"]}


def read_batch(llm, stories: list[dict], headlines: dict[int, list[str]]) -> list[dict]:
    lines = "\n".join(
        f"[{i + 1}] {s['title']}" + "".join(f"\n    also: {h}" for h in headlines.get(s["id"], [])[:3] if h != s["title"])
        for i, s in enumerate(stories))
    kinds = "\n".join(f"- {k}: {v}" for k, v in KINDS.items())
    prompt = f"""You watch the world's freight network: ships, ports, canals and straits, cargo planes, freight rail,
trucking, border crossings, and trade rules.

Stories (headlines, numbered):
{lines}

For each story:
- disruption: true only if the story reports one of these, happening now or formally announced:
  * a port, terminal, canal, strait, airport, rail line, road or border crossing closed, blocked, restricted or slowed;
  * a merchant ship, its cargo or crew attacked, hit, seized, detained, sunk, aground or on fire;
  * workers at ports, railways, cargo airlines or trucking on strike, or a strike date announced;
  * a government imposing, raising, announcing or lifting a tariff, quota, export or import ban, or trade sanction;
  * a storm, flood, quake or fire shutting down ports or transport.
  Everything else is false: fuel prices or fuel taxes, company results, freight rates, analysis of past events,
  opinion, politicians' comments, and fighting on land or between navies unless merchant ships or ports are hit.
- kind, from this list:
{kinds}
- place: the most specific place it happens: the port, strait, canal, sea, border crossing, rail line's main city,
  or airport, as a name you could find on a map ("Strait of Hormuz", not "Iran"). For a tariff or ban: the
  country imposing it. "" if none.
- country: ISO 3166 two letter code of that place, or "".
- severity: 1 minor or local, 2 serious (a major port, days of delay, a ship hit), 3 major (a chokepoint shut,
  a national strike, a big tariff between large economies).
- status: ongoing, ended, or threatened (announced or warned of, not happened yet).
- summary: one plain sentence on what happened, in English.
Answer for every number."""
    out = llm(prompt, schema())
    return [r for r in out.get("stories") or [] if isinstance(r.get("n"), int) and 1 <= r["n"] <= len(stories)]


# Waterways named in headlines, and where to put them. Straits and canals use PortWatch's points
# when loaded (the names match); seas have no point of their own there.
WATERS = [
    (re.compile(r"\bhormuz\b", re.I), "Strait of Hormuz", (26.30, 56.86)),
    (re.compile(r"\bbab[ -]?(el|al)[ -]?mandab|\bbab[ -]?el[ -]?mandeb", re.I), "Bab el-Mandeb Strait", (12.79, 43.35)),
    (re.compile(r"\bsuez\b", re.I), "Suez Canal", (30.59, 32.44)),
    (re.compile(r"\bpanama canal\b", re.I), "Panama Canal", (9.12, -79.77)),
    (re.compile(r"\bmalacca\b", re.I), "Malacca Strait", (1.52, 102.67)),
    (re.compile(r"\bbosp(h)?orus\b", re.I), "Bosporus Strait", (41.17, 29.09)),
    (re.compile(r"\bkerch\b", re.I), "Kerch Strait", (45.27, 36.54)),
    (re.compile(r"\btaiwan strait\b", re.I), "Taiwan Strait", (24.72, 119.83)),
    (re.compile(r"\b(gibraltar)\b", re.I), "Gibraltar Strait", (35.94, -5.75)),
    (re.compile(r"\b(english channel|dover strait|strait of dover)\b", re.I), "Dover Strait", (51.03, 1.51)),
    (re.compile(r"\bred sea\b", re.I), "Red Sea", (18.5, 39.8)),
    (re.compile(r"\bblack sea\b", re.I), "Black Sea", (43.3, 34.0)),
    (re.compile(r"\bbaltic\b", re.I), "Baltic Sea", (57.0, 19.0)),
    (re.compile(r"\bsouth china sea\b", re.I), "South China Sea", (13.0, 114.0)),
    (re.compile(r"\bgulf of aden\b", re.I), "Gulf of Aden", (12.5, 48.0)),
    (re.compile(r"\bgulf of oman\b", re.I), "Gulf of Oman", (24.5, 58.5)),
    (re.compile(r"\b(persian|arabian) gulf\b", re.I), "Persian Gulf", (26.8, 51.5)),
]


def waterway(conn, text: str) -> tuple[float, float, str] | None:
    for rx, name, (lat, lon) in WATERS:
        if rx.search(text or ""):
            row = conn.execute("SELECT lat, lon FROM logistics_sites WHERE kind = 'chokepoint' AND name = %s", (name,)).fetchone()
            return (row["lat"], row["lon"], name) if row else (lat, lon, name)
    return None


def locate(conn, place: str, country: str, story: dict, text: str = "") -> tuple[float, float, str] | None:
    """Where an event goes: a known freight site, then a waterway the headlines name (a strait
    rather than the country beside it), then a town, then the country, then the story's dot."""
    place, country = (place or "").strip(), (country or "").strip().upper()[:2]
    water = waterway(conn, place) or waterway(conn, text)
    g = gazetteer()
    if water and (not place or waterway(conn, place) or g.by_name(place) in g.countries.values() or place.upper() == country):
        return water
    if place:
        for name in (place, place.removeprefix("Port of ").removeprefix("port of "), place.split(",")[0]):
            row = conn.execute(
                """SELECT lat, lon, name FROM logistics_sites WHERE lower(name) = lower(%s) AND (%s = '' OR country = %s OR country IS NULL)
                   ORDER BY (kind = 'chokepoint') DESC, rank DESC LIMIT 1""", (name, country, country)).fetchone()
            if row:
                return row["lat"], row["lon"], row["name"]
        for p in g.find(place, limit=4):
            if p.kind == "city" and (not country or p.country == country):
                return p.lat, p.lon, p.name
    if story.get("place_lat") is not None and (not country or story.get("place_country") in (country, None)):
        return story["place_lat"], story["place_lon"], story["place_name"]
    if water:
        return water
    c = g.country(country) if country else None
    if c:
        return c.lat, c.lon, c.name
    if story.get("place_lat") is not None:
        return story["place_lat"], story["place_lon"], story["place_name"]
    return None


def run_events(conn: psycopg.Connection, llm=None, max_seconds: float = 120) -> int:
    stories = conn.execute(PENDING, (FREIGHT, PER_RUN)).fetchall()
    conn.commit()
    if not stories:
        return 0
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    heads: dict[int, list[str]] = {}
    for r in conn.execute(
            """SELECT story_id, coalesce(title_en, title) AS t FROM (
                 SELECT story_id, title_en, title, row_number() OVER (PARTITION BY story_id ORDER BY published_at DESC) rn
                 FROM items WHERE story_id = ANY(%s)) x WHERE rn <= 4""", ([s["id"] for s in stories],)):
        heads.setdefault(r["story_id"], []).append(r["t"])
    conn.commit()
    started, done, found = time.time(), 0, 0
    for i in range(0, len(stories), BATCH):
        if time.time() - started > max_seconds:
            break
        batch = stories[i:i + BATCH]
        try:
            answers = read_batch(llm, batch, heads)
        except Exception as e:  # noqa: BLE001
            log.warning("shipping events: model call failed: %s", e)
            break
        for a in answers:
            s = batch[a["n"] - 1]
            found += store(conn, s, a, " ".join([s["title"], *heads.get(s["id"], [])]))
            done += 1
        conn.commit()
    if done:
        log.info("shipping events: read %d stories, %d disruptions on the map", done, found)
    return done


# A kind the headlines must back up with at least one of its words, so a fuel tax cut is not a
# tariff and a battle on a coast is not an attack on a ship.
KIND_WORDS = {
    "tariff": r"tariff|dut(y|ies)|levy|levies|quota|import tax|trade (war|deal|measure|restriction)",
    "sanctions": r"sanction|embargo|export ban|import ban|\bban(s|ned)?\b|export control|blacklist",
    "attack_on_ship": r"ship|vessel|tanker|freighter|carrier|cargo|boat|crew|sailor|seafarer|merchant|maritime|ukmto",
    "seizure": r"ship|vessel|tanker|freighter|carrier|cargo|boat|crew|container|seiz|detain|impound",
    "labour_strike": r"strike|walkout|walk out|stoppage|industrial action|union",
}


def backed(kind: str, text: str) -> bool:
    words = KIND_WORDS.get(kind)
    return not words or re.search(words, text or "", re.I) is not None


def store(conn, s: dict, a: dict, text: str = "") -> int:
    key = f"story:{s['id']}"
    text = text or s["title"]
    ok = bool(a.get("disruption")) and backed(a.get("kind") or "", text)
    where = locate(conn, a.get("place") or "", a.get("country") or "", s, text) if ok else None
    if not where:
        # Not a disruption (or nowhere to put it): remember it was read, off the map.
        conn.execute(
            """INSERT INTO shipping_events (key, story_id, kind, title, source, items, status, info, updated_at)
               VALUES (%s, %s, 'none', %s, 'news', %s, 'none', %s, now())
               ON CONFLICT (key) DO UPDATE SET kind = 'none', status = 'none', title = EXCLUDED.title, items = EXCLUDED.items,
                 lat = NULL, lon = NULL, info = EXCLUDED.info, updated_at = now()""",
            (key, s["id"], s["title"], s["item_count"], Jsonb({"disruption": bool(a.get("disruption"))})))
        return 0
    kind = a.get("kind") if a.get("kind") in KINDS else "other"
    country = (a.get("country") or "").strip().upper()[:2] or None
    conn.execute(
        """INSERT INTO shipping_events (key, story_id, kind, title, summary, place, country, lat, lon, severity, status, source,
                                        started_at, items, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'news', %s, %s, now())
           ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind, title = EXCLUDED.title, summary = EXCLUDED.summary,
             place = EXCLUDED.place, country = EXCLUDED.country, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
             severity = EXCLUDED.severity, status = EXCLUDED.status, items = EXCLUDED.items, updated_at = now()""",
        (key, s["id"], kind, s["title"], (a.get("summary") or "")[:400] or None, where[2] or a.get("place") or None, country,
         where[0], where[1], int(a.get("severity") or 1), a.get("status") or "ongoing", s["first_seen"], s["item_count"]))
    return 1
