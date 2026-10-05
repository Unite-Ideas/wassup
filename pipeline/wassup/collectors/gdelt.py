"""GDELT 2.0 Global Knowledge Graph. Every 15 minutes GDELT publishes the articles it saw
worldwide, already tagged with themes, locations, people and organizations. The translingual
feed covers 65 languages.

Field reference: http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf
"""
from __future__ import annotations

import csv
import io
import logging
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from ..config import load_yaml, settings
from ..db import kv_get, kv_set
from ..geo import Place, gazetteer
from ..outlets import tier_for
from ..text import clean_title, is_junk_title
from .base import Collector, RawItem, ensure_source, mark_polled, store_items

log = logging.getLogger(__name__)
csv.field_size_limit(sys.maxsize)

BASE = "http://data.gdeltproject.org/gdeltv2"
FEEDS = {"english": "lastupdate.txt", "translingual": "lastupdate-translation.txt"}
FILE_SUFFIX = {"english": ".gkg.csv.zip", "translingual": ".translation.gkg.csv.zip"}
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
_SRCLC = re.compile(r"srclc:(\w+)")
# GDELT uses ISO 639-3 in translation info. The common ones, mapped to the 2 letter codes used elsewhere.
LANG3 = {"rus": "ru", "ukr": "uk", "fas": "fa", "per": "fa", "ara": "ar", "heb": "he", "tur": "tr", "zho": "zh",
         "chi": "zh", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "ita": "it", "por": "pt",
         "pol": "pl", "jpn": "ja", "kor": "ko", "hin": "hi", "urd": "ur", "ben": "bn", "ind": "id", "vie": "vi",
         "tha": "th", "nld": "nl", "dut": "nl", "swe": "sv", "ron": "ro", "rum": "ro", "hun": "hu", "ces": "cs",
         "ell": "el", "gre": "el", "srp": "sr", "hrv": "hr", "bul": "bg", "bel": "be", "kaz": "kk", "aze": "az",
         "hye": "hy", "kat": "ka", "pus": "ps", "kur": "ku", "som": "so", "amh": "am", "swa": "sw", "eng": "en"}


ACRONYMS = set("""nato un eu fbi cia nsa imf idf irgc iaea icc icj who opec asean brics osce unhcr unicef unrwa wto
g7 g20 doj dhs ice cbp nasa sec ftc fda cdc gop bbc cnn ap afp uae us uk usa ussr rsf sdf pkk ypg hts isis isil
aaro odni dod nhs mi6 mi5 fsb gru svr sbu kgb ndaa ai""".split())


def _title_case(name: str) -> str:
    """GDELT lowercases names. Restore capitals, keeping well known acronyms upper case."""
    return " ".join(w.upper() if w in ACRONYMS else w.capitalize() for w in name.lower().split())


def parse_locations(field: str, limit: int = 4) -> list[Place]:
    """V2ENHANCEDLOCATIONS: Type#FullName#CountryCode#ADM1#ADM2#Lat#Long#FeatureID#CharOffset.
    Earlier mentions are more central to the article, so sort by offset."""
    gaz = gazetteer()
    entries = []
    for block in filter(None, field.split(";")):
        f = block.split("#")
        if len(f) < 9 or not f[5] or not f[6]:
            continue
        try:
            loc_type, lat, lon, offset = int(f[0]), float(f[5]), float(f[6]), int(f[8] or 0)
        except ValueError:
            continue
        entries.append((offset, loc_type, f[1], gaz.fips_to_iso.get(f[2]), lat, lon, f[7]))
    entries.sort()
    out: dict[str, Place] = {}
    for _off, loc_type, name, iso, lat, lon, feature in entries:
        if loc_type == 1 and iso:  # country
            p = gaz.country(iso) or Place(f"cc:{iso}", name, iso, "country", lat, lon)
        elif loc_type in (2, 5):  # state or province
            p = Place(f"gdelt:{feature or name}", name.split(",")[0], iso, "region", lat, lon)
        else:  # city or landmark; reuse the gazetteer city when one is close
            p = gaz.snap_city(lat, lon, iso) or Place(f"gdelt:{feature or name}", name.split(",")[0], iso, "city", lat, lon)
        out.setdefault(p.key, p)
        if len(out) >= limit:
            break
    places = list(out.values())
    city_countries = {p.country for p in places if p.kind != "country"}
    return [p for p in places if not (p.kind == "country" and p.country in city_countries)] or places


def parse_gkg(data: bytes, theme_prefixes: list[str], min_themes: int = 1, feed: str = "english") -> list[RawItem]:
    prefixes = tuple(theme_prefixes)
    items = []
    text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace", newline="")
    for row in csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) < 27 or row[2] != "1":  # collection 1 is web articles
            continue
        themes = sorted({t.split(",")[0] for t in row[7].split(";") if t})
        matched = [t for t in themes if t.startswith(prefixes)]
        if len(matched) < min_themes:
            continue
        m = _TITLE.search(row[26])
        if not m or not m.group(1).strip():
            continue
        try:
            published = datetime.strptime(row[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lang = "en"
        if feed == "translingual":
            lm = _SRCLC.search(row[25])
            lang = LANG3.get(lm.group(1), lm.group(1)) if lm else None
        tier, state = tier_for(row[3] or row[4])
        persons = [("person", _title_case(n)) for n in dict.fromkeys(filter(None, row[11].split(";")))][:8]
        orgs = [("org", _title_case(n)) for n in dict.fromkeys(filter(None, row[13].split(";")))][:8]
        try:
            tone = float(row[15].split(",")[0]) if row[15] else None
        except ValueError:
            tone = None
        title = clean_title(m.group(1))[:500]
        if is_junk_title(title):
            continue
        items.append(RawItem(
            url=row[4], title=title, published_at=published, language=lang,
            outlet=row[3], outlet_tier=tier, outlet_state=state,
            places=parse_locations(row[10]), entities=persons + orgs,
            meta={"themes": matched[:25], "tone": tone, "gdelt_id": row[0], "feed": feed},
        ))
    return items


class GdeltCollector(Collector):
    key = "gdelt"
    interval_s = 300  # GDELT updates every 15 minutes; checking more often just catches it sooner

    def __init__(self):
        cfg = load_yaml("sources.yaml").get("gdelt") or {}
        self.enabled = cfg.get("enabled", True)
        self.feeds = ["english"] + (["translingual"] if cfg.get("translingual", True) else [])
        self.prefixes = cfg.get("theme_prefixes") or []
        self.min_themes = int(cfg.get("min_themes_matched", 1))
        self.client = httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": settings().http_user_agent})

    def _latest_stamp(self, feed: str) -> str:
        r = self.client.get(f"{BASE}/{FEEDS[feed]}")
        r.raise_for_status()
        for line in r.text.splitlines():
            url = line.split()[-1]
            if url.endswith(FILE_SUFFIX[feed]):
                return url.rsplit("/", 1)[1][:14]
        raise ValueError(f"no gkg file in {FEEDS[feed]}")

    def _download(self, stamp: str, feed: str) -> bytes | None:
        r = self.client.get(f"{BASE}/{stamp}{FILE_SUFFIX[feed]}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            return zf.read(zf.namelist()[0])

    def run(self, conn: psycopg.Connection) -> int:
        if not self.enabled:
            return 0
        total = 0
        for feed in self.feeds:
            sid = ensure_source(conn, f"gdelt:{feed}", f"GDELT ({feed})", "gdelt", BASE, None, None, "U")
            conn.commit()
            try:
                latest = self._latest_stamp(feed)
                last = kv_get(conn, f"gdelt:{feed}:last")
                for stamp in _stamps_between(last, latest, settings().gdelt_backfill_files):
                    data = self._download(stamp, feed)
                    if data is None:
                        continue
                    items = parse_gkg(data, self.prefixes, self.min_themes, feed)
                    n = store_items(conn, sid, items)
                    kv_set(conn, f"gdelt:{feed}:last", stamp)
                    conn.commit()
                    total += n
                    log.info("gdelt %s %s: %d kept, %d new", feed, stamp, len(items), n)
                mark_polled(conn, sid)
                conn.commit()
            except Exception as e:
                conn.rollback()
                mark_polled(conn, sid, f"{type(e).__name__}: {e}"[:300])
                conn.commit()
                log.exception("gdelt %s failed", feed)
        return total


def _stamps_between(last: str | None, latest: str, backfill: int) -> list[str]:
    """15 minute file stamps after `last` up to `latest`. With no history, the last `backfill`
    files. Never more than one day of files in one go."""
    fmt = "%Y%m%d%H%M%S"
    end = datetime.strptime(latest, fmt)
    start = datetime.strptime(last, fmt) + timedelta(minutes=15) if last else end - timedelta(minutes=15 * (backfill - 1))
    start = max(start, end - timedelta(days=1))
    out = []
    while start <= end:
        out.append(start.strftime(fmt))
        start += timedelta(minutes=15)
    return out
