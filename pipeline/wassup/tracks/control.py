"""Who holds what: control maps for wars and insurgencies (config/conflicts.yaml).

Wikipedia's detailed control maps are Lua modules: a list of places, each with coordinates and
a marker image whose colour says who holds it ("Location dot red.svg"), and a legend that says
which colour is which side, in the module's caption or on its documentation page. Colours
change meaning over time (grey on the Syria map meant one side before December 2024 and another
after), so every map is read with the legend as it was on that date.

Each map becomes a snapshot of a "control" track:
- place: every town, base, airport, port, hill or rural presence, with its holder (or the two
  sides contesting it, or sharing it);
- held: shaded areas, one per side: each place's share of the country (its Voronoi cell), cut to
  a circle around it so empty desert is not handed to the nearest town.

History is filled in gently (Wikipedia asks bots to go slow): daily for recent months, weekly
before that.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..config import load_yaml

log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"
RAW = "https://en.wikipedia.org/w/index.php"
UA = "WassupNewsBot/0.1 (personal news research; https://github.com/Unite-Ideas/wassup)"
PAUSE_S = 2.0

# Colour words in marker file names, longest first, and how each is drawn.
COLORS = {
    "darkred": "#9b1c1c", "dark red": "#9b1c1c", "deeppink": "#ff1493", "lightgrey": "#c9d1d9", "lightgreen": "#9be37a",
    "darkgreen": "#1f7a3a", "red": "#e53935", "lime": "#8ee000", "green": "#22b04a", "blue": "#2f7bf0", "black": "#6b6b6b",
    "grey": "#a3abb3", "gray": "#a3abb3", "yellow": "#f5d90a", "orange": "#ff8c1a", "purple": "#9b59d0", "magenta": "#e040fb",
    "pink": "#ff8fb8", "teal": "#14a89c", "cyan": "#22d3ee", "brown": "#9c6b4e", "white": "#f4f4f4", "olive": "#a5a52a",
    "navy": "#3b4fc4", "maroon": "#a0253b", "violet": "#b47cff", "gold": "#e0b000",
}
ALIASES = {"lime": "green", "green": "lime", "gray": "grey", "grey": "gray", "dark red": "darkred", "darkred": "dark red"}
_COLOR_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted((re.escape(c) for c in COLORS), key=len, reverse=True)) + r")(?![a-z])")
RADIUS_KM = {"settlement": 14, "rural": 22, "base": 8, "airport": 8, "port": 8, "hill": 6, "siege": 10}


def norm_file(name: str) -> str:
    return " ".join(name.replace("_", " ").strip().lower().split())


def colors_in(name: str) -> list[str]:
    n = norm_file(name)
    n = re.sub(r"\.(svg|png|gif)$", "", n)
    n = n.replace("+", " ").replace("-", " ")
    return [m.group(1) for m in _COLOR_RE.finditer(n)]


def kind_of(name: str) -> str | None:
    n = norm_file(name)
    if "anim" in n:
        return "contested"
    if "ctl2" in n or "ctl3" in n:
        return "mixed"
    if "map-circle" in n:
        return "siege"
    if "map-arc" in n:
        return "pressure"
    if n.startswith("abm"):
        return "base"
    if "fighter-jet" in n or "helicopter" in n:
        return "airport"
    if "anchor" in n:
        return "port"
    if "map-peak" in n:
        return "hill"
    if "4x4dot" in n:
        return "rural"
    if "dot" in n:
        return "settlement"
    return None  # oil, dams, border posts, pictures: infrastructure, not control


# --- reading a module ------------------------------------------------------------------------

_BLOCK_COMMENT = re.compile(r"--\[(=*)\[.*?\]\1\]", re.S)
_LINE_COMMENT = re.compile(r"(^|[,{}])\s*--[^\n]*", re.M)
_PAIR = re.compile(r"""(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|-?[\d.]+)""")


def wikitext_plain(s: str) -> str:
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s or "")
    s = re.sub(r"'{2,}|<[^>]+>|\{\{[^}]*\}\}|&nbsp;?|&[a-z]+;", " ", s)
    return " ".join(s.split()).strip(" ,;:-")


def parse_marks(lua: str) -> list[dict]:
    lua = _LINE_COMMENT.sub(r"\1", _BLOCK_COMMENT.sub("", lua))
    start = lua.find("marks")
    end = lua.find("containerArgs")
    body = lua[start:end if end > start else len(lua)]
    out = []
    for block in re.finditer(r"\{([^{}]*\blat\b[^{}]*)\}", body):
        kv = {k.lower(): v.strip("\"'") for k, v in _PAIR.findall(block.group(1))}
        try:
            lat, lon = float(kv["lat"]), float(kv.get("long") or kv.get("lon"))
        except (KeyError, ValueError, TypeError):
            continue
        if not kv.get("mark") or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        try:
            size = float(kv.get("marksize") or 6)
        except ValueError:
            size = 6.0
        out.append({"lat": lat, "lon": lon, "mark": kv["mark"], "size": size, "label": wikitext_plain(kv.get("label", "")),
                    "link": kv.get("link") or ""})
    return out


def parse_legend(text: str) -> dict[str, str]:
    """Marker file -> side, from a caption ("*[[File:X|11px]] Houthis and allies", or several on one
    line separated by ';') and from documentation tables (a row naming the side, then its icons)."""
    legend: dict[str, str] = {}
    cap = re.search(r"caption\s*=\s*\[(=*)\[(.*?)\]\1\]", text, re.S)
    for chunk in ([cap.group(2)] if cap else []) + [text]:
        parts = re.split(r"\[\[File:", chunk)
        for part in parts[1:]:
            fname, _, rest = part.partition("|")
            after = rest.split("]]", 1)[1] if "]]" in rest else ""
            label = wikitext_plain(re.split(r"\n|;|<br\s*/?>", after, maxsplit=1)[0])
            label = re.sub(r"^(under (the )?control of (the )?)", "", label, flags=re.I).strip()
            if len(re.sub(r"[^A-Za-z]", "", label)) >= 2 and norm_file(fname) not in legend and kind_of(fname) in ("settlement", "contested", "mixed"):
                legend[norm_file(fname)] = label[:160]
    # Documentation tables: |Side name  newline  |[[File:dot...]]
    for row in re.split(r"\n\|-", text):
        cells = [c.strip() for c in re.split(r"\n[|!]", "\n" + row) if c.strip()]
        name = next((c for c in cells if "[[File:" not in c and len(wikitext_plain(c)) >= 2), None)
        f = next((re.search(r"\[\[File:([^|\]]+)", c).group(1) for c in cells if "[[File:" in c), None)
        if name and f and norm_file(f) not in legend and kind_of(f) == "settlement":
            legend[norm_file(f)] = wikitext_plain(name)[:160]
    return legend


def sides(legend: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(file -> side, colour -> side) for single-colour markers."""
    by_color: dict[str, str] = {}
    by_file: dict[str, str] = {}
    for f, label in legend.items():
        cs = colors_in(f)
        if kind_of(f) == "settlement" and len(cs) == 1:
            by_file[f] = label
            by_color.setdefault(cs[0], label)
    return by_file, by_color


def holder(f: str, color: str, by_file: dict, by_color: dict) -> str | None:
    if f in by_file:
        return by_file[f]
    return by_color.get(color) or by_color.get(ALIASES.get(color, ""))


def classify(marks: list[dict], legend: dict[str, str]) -> list[dict]:
    by_file, by_color = sides(legend)
    out = []
    for m in marks:
        kind = kind_of(m["mark"])
        if kind is None:
            continue
        f, cs = norm_file(m["mark"]), colors_in(m["mark"])
        if not cs:
            continue
        held = [h for h in (holder(f if len(cs) == 1 else "", c, by_file, by_color) for c in cs) if h]
        if not held:
            continue
        side = held[0] if kind not in ("contested", "mixed") else None
        out.append({**m, "kind": kind, "side": side, "sides": list(dict.fromkeys(held)), "colors": cs})
    return out


def palette(legend: dict[str, str]) -> dict[str, dict]:
    """Side -> colour to draw it in (the map's own colour, so it looks familiar)."""
    by_file, by_color = sides(legend)
    out = {}
    for color, side in by_color.items():
        out.setdefault(side, {"color": color, "hex": COLORS.get(color, "#888888")})
    for f, side in by_file.items():
        c = colors_in(f)[0]
        out.setdefault(side, {"color": c, "hex": COLORS.get(c, "#888888")})
    return out


# --- Wikipedia ------------------------------------------------------------------------------

class Wiki:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=60, headers={"User-Agent": UA})
        self.last = 0.0

    def _get(self, url: str, params: dict) -> httpx.Response:
        wait = PAUSE_S - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            r = self.client.get(url, params=params)
            self.last = time.time()
            if r.status_code != 429:
                r.raise_for_status()
                return r
            time.sleep(30 * (attempt + 1))  # asked to slow down
        r.raise_for_status()
        return r

    def latest(self, titles: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(titles), 40):
            d = self._get(API, {"action": "query", "prop": "revisions", "rvprop": "ids|timestamp", "format": "json",
                                "formatversion": "2", "titles": "|".join(titles[i:i + 40])}).json()
            for p in d.get("query", {}).get("pages", []):
                if p.get("revisions"):
                    rv = p["revisions"][0]
                    out[p["title"]] = {"revid": rv["revid"], "at": _ts(rv["timestamp"])}
        return out

    def history(self, title: str, since: datetime) -> list[dict]:
        """All revisions back to `since`, newest first."""
        revs, cont = [], {}
        while True:
            d = self._get(API, {"action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp",
                                "rvlimit": "500", "rvend": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": "json",
                                "formatversion": "2", **cont}).json()
            for p in d.get("query", {}).get("pages", []):
                revs += [{"revid": r["revid"], "at": _ts(r["timestamp"])} for r in p.get("revisions") or []]
            if "continue" not in d:
                return revs
            cont = d["continue"]

    def raw(self, title: str, revid: int | None = None) -> str:
        params = {"title": title, "action": "raw"}
        if revid:
            params["oldid"] = str(revid)
        return self._get(RAW, params).text

    def raw_as_of(self, title: str, at: datetime) -> str:
        """A page's text as it was at a moment (for documentation pages holding a legend)."""
        d = self._get(API, {"action": "query", "prop": "revisions", "titles": title, "rvlimit": "1", "rvprop": "content",
                            "rvslots": "main", "rvdir": "older", "rvstart": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "format": "json", "formatversion": "2"}).json()
        for p in d.get("query", {}).get("pages", []):
            for r in p.get("revisions") or []:
                return r["slots"]["main"]["content"]
        return ""


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def wanted_revisions(revs: list[dict], now: datetime, daily_days: int, weekly_days: int) -> list[dict]:
    """The last revision of each day for recent months, of each week before that."""
    pick: dict[str, dict] = {}
    for r in revs:  # newest first: the first one seen for a day or week is that day's last
        age = (now - r["at"]).days
        if age > weekly_days:
            continue
        key = r["at"].strftime("%Y-%m-%d") if age <= daily_days else r["at"].strftime("%G-W%V")
        pick.setdefault(key, r)
    return sorted(pick.values(), key=lambda r: r["at"], reverse=True)


# --- storing ---------------------------------------------------------------------------------

def ensure_track(conn, c: dict) -> int:
    title = f"Module:{c['module']}"
    row = conn.execute(
        """INSERT INTO tracks (key, name, kind, desk, source, description, meta)
           VALUES (%s, %s, 'control', %s, %s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, desk = EXCLUDED.desk, source = EXCLUDED.source,
             meta = tracks.meta || EXCLUDED.meta RETURNING id""",
        (f"control-{c['key']}", f"{c['name']}: who holds what", c.get("desk"), "Wikipedia",
         "Who holds each place, from Wikipedia's detailed control map (volunteer edited; can lag events).",
         Jsonb({"module": title, "url": f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"}))).fetchone()
    return row["id"]


def store_snapshot(conn, track: int, title: str, rev: dict, lua: str, doc: str | None) -> dict:
    legend = parse_legend(lua)
    if doc:
        for f, side in parse_legend(doc).items():
            legend.setdefault(f, side)
    places = classify(parse_marks(lua), legend)
    pal = palette(legend)
    day = rev["at"].date()
    # One map a day: a later edit the same day replaces the earlier one.
    conn.execute("DELETE FROM track_snapshots WHERE track_id = %s AND observed_at::date = %s", (track, day))
    counts: dict[str, int] = {}
    for p in places:
        if p["side"]:
            counts[p["side"]] = counts.get(p["side"], 0) + 1
    stats = {"revision": rev["revid"], "edited_at": rev["at"].isoformat(), "places": len(places),
             "contested": sum(p["kind"] == "contested" for p in places),
             "sides": {s: {**pal.get(s, {"color": None, "hex": "#888888"}), "places": n} for s, n in sorted(counts.items(), key=lambda x: -x[1])},
             "source_url": f"https://en.wikipedia.org/w/index.php?title={quote(title.replace(' ', '_'))}&oldid={rev['revid']}"}
    snap = conn.execute(
        "INSERT INTO track_snapshots (track_id, observed_at, source_ref, stats) VALUES (%s, %s, %s, %s) RETURNING id",
        (track, rev["at"], str(rev["revid"]), Jsonb(stats))).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom, label, props)
               VALUES (%s, %s, %s, 'place', ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s)""",
            [(track, snap, rev["at"], p["lon"], p["lat"], p["label"][:200],
              Jsonb({"side": p["side"], "sides": p["sides"], "kind": p["kind"], "size": p["size"], "link": p["link"],
                     "hex": pal.get(p["side"], {}).get("hex") if p["side"] else None,
                     "hexes": [pal.get(s, {}).get("hex", "#888888") for s in p["sides"]]})) for p in places])
    held_areas(conn, track, snap, rev["at"], pal)
    return stats


def held_areas(conn, track: int, snap: int, at: datetime, pal: dict) -> None:
    """Each side's area: the Voronoi cell of each of its places (so neighbouring sides meet
    halfway), cut to a circle around the place (so empty land is not handed to the nearest town),
    then merged per side. Indexed temporary tables keep big maps fast (Syria: 7,000 places in
    under two seconds)."""
    radius = "CASE kind " + " ".join(f"WHEN '{k}' THEN {v * 1000}" for k, v in RADIUS_KM.items()) + " ELSE 10000 END"
    conn.execute("DROP TABLE IF EXISTS _ctl_p, _ctl_cells")
    conn.execute(
        """CREATE TEMP TABLE _ctl_p ON COMMIT DROP AS
           SELECT DISTINCT ON (ST_SnapToGrid(geom, 0.0005)) geom, props->>'side' AS side, props->>'kind' AS kind
           FROM track_observations WHERE snapshot_id = %s AND category = 'place'
             AND props->>'side' IS NOT NULL AND props->>'kind' <> 'pressure'""", (snap,))
    conn.execute("CREATE INDEX ON _ctl_p USING gist (geom)")
    conn.execute("CREATE TEMP TABLE _ctl_cells ON COMMIT DROP AS SELECT (ST_Dump(ST_VoronoiPolygons(ST_Collect(geom)))).geom AS cell FROM _ctl_p")
    rows = conn.execute(
        f"""WITH own AS (
              SELECT p.side, ST_Intersection(c.cell, ST_Buffer(p.geom::geography, {radius}, 4)::geometry) AS g
              FROM _ctl_cells c JOIN LATERAL (SELECT * FROM _ctl_p p WHERE ST_Intersects(c.cell, p.geom) LIMIT 1) p ON true)
            SELECT side, ST_AsText(ST_SimplifyPreserveTopology(ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Union(g)), 3)), 0.002)) AS wkt
            FROM own GROUP BY side""").fetchall()
    conn.execute("DROP TABLE IF EXISTS _ctl_p, _ctl_cells")
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom, label, props)
               VALUES (%s, %s, %s, 'held', ST_GeomFromText(%s, 4326), %s, %s)""",
            [(track, snap, at, r["wkt"], r["side"], Jsonb({"side": r["side"], "hex": pal.get(r["side"], {}).get("hex", "#888888")}))
             for r in rows if r["wkt"] and not r["wkt"].endswith("EMPTY")])


# --- the lane --------------------------------------------------------------------------------

def run_control(conn: psycopg.Connection, wiki: Wiki | None = None, max_seconds: float = 240) -> int:
    cfg = load_yaml("conflicts.yaml")
    w = cfg.get("wikipedia") or {}
    conflicts = cfg.get("conflicts") or []
    if not conflicts:
        return 0
    wiki = wiki or Wiki()
    started, done = time.time(), 0
    tracks = {c["key"]: ensure_track(conn, c) for c in conflicts}
    conn.commit()
    now = datetime.now(timezone.utc)
    # New edits: one request for all of them, every few hours.
    due = [c for c in conflicts
           if not _recent(conn, tracks[c["key"]], "checked_at", timedelta(hours=float(w.get("check_hours", 3))))]
    if due:
        try:
            latest = wiki.latest([f"Module:{c['module']}" for c in due])
        except Exception as e:
            log.warning("control maps: could not reach Wikipedia: %s", e)
            return 0
        for c in due:
            title = f"Module:{c['module']}"
            t = tracks[c["key"]]
            rev = latest.get(title)
            _meta(conn, t, checked_at=now.isoformat())
            if rev and not conn.execute("SELECT 1 FROM track_snapshots WHERE track_id = %s AND source_ref = %s",
                                        (t, str(rev["revid"]))).fetchone():
                done += _fetch_and_store(conn, wiki, t, title, rev)
            conn.commit()
    # History, a few maps per conflict per run.
    per_run = int(w.get("maps_per_run", 8))
    for c in conflicts:
        if time.time() - started > max_seconds:
            break
        t = tracks[c["key"]]
        title = f"Module:{c['module']}"
        meta = conn.execute("SELECT meta FROM tracks WHERE id = %s", (t,)).fetchone()["meta"]
        if not meta.get("history_listed"):
            try:
                revs = wiki.history(title, now - timedelta(days=int(w.get("weekly_days", 730))))
            except Exception as e:
                log.warning("control maps: history of %s: %s", title, e)
                continue
            want = wanted_revisions(revs, now, int(w.get("daily_days", 120)), int(w.get("weekly_days", 730)))
            _meta(conn, t, history_listed=now.isoformat(), pending=[{"revid": r["revid"], "at": r["at"].isoformat()} for r in want])
            conn.commit()
            meta = conn.execute("SELECT meta FROM tracks WHERE id = %s", (t,)).fetchone()["meta"]
        pending = meta.get("pending") or []
        have = {r["source_ref"] for r in conn.execute("SELECT source_ref FROM track_snapshots WHERE track_id = %s", (t,))}
        todo = [p for p in pending if str(p["revid"]) not in have][:per_run]
        for p in todo:
            if time.time() - started > max_seconds:
                break
            done += _fetch_and_store(conn, wiki, t, title, {"revid": p["revid"], "at": _ts(p["at"])})
            conn.commit()
        if not todo and pending:
            _meta(conn, t, pending=[])
            conn.commit()
    if done:
        log.info("control maps: %d maps read", done)
    return done


def _fetch_and_store(conn, wiki: Wiki, track: int, title: str, rev: dict) -> int:
    try:
        lua = wiki.raw(title, rev["revid"])
        doc = None
        if not re.search(r"caption\s*=\s*\[=*\[.*?\[\[File:", lua, re.S):
            doc = wiki.raw_as_of(f"{title}/doc", rev["at"])  # the legend lives on the documentation page
        stats = store_snapshot(conn, track, title, rev, lua, doc)
        if not stats["sides"]:
            log.warning("control maps: no legend found for %s revision %s", title, rev["revid"])
        return 1
    except Exception as e:
        conn.rollback()
        log.warning("control maps: %s revision %s: %s", title, rev["revid"], e)
        return 0


def _recent(conn, track: int, key: str, within: timedelta) -> bool:
    v = conn.execute("SELECT meta->>%s AS v FROM tracks WHERE id = %s", (key, track)).fetchone()["v"]
    return bool(v) and datetime.now(timezone.utc) - datetime.fromisoformat(v) < within


def _meta(conn, track: int, **values) -> None:
    conn.execute("UPDATE tracks SET meta = meta || %s WHERE id = %s", (Jsonb(values), track))
