"""RSS and Atom feeds listed in config/sources.yaml."""
from __future__ import annotations

import calendar
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser
import httpx
import psycopg

from ..config import load_yaml, settings
from ..geo import gazetteer
from ..text import clean, clean_title, detect_language, is_junk_title
from .base import Collector, RawItem, ensure_source, mark_polled, store_items

log = logging.getLogger(__name__)

# Sources whose stories are about Washington unless the text says otherwise.
DEFAULT_PLACE = {"whitehouse": "Washington", "whitehouse_actions": "Washington", "state_dept": "Washington",
                 "dod_releases": "Washington", "roll_call": "Washington", "the_hill": "Washington",
                 "nyt_politics": "Washington", "npr_politics": "Washington", "guardian_us_politics": "Washington",
                 "fox_politics": "Washington"}


def _when(entry) -> datetime:
    for k in ("published_parsed", "updated_parsed"):
        t = entry.get(k)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


def parse_feed(content: bytes, src: dict) -> list[RawItem]:
    gaz = gazetteer()
    feed = feedparser.parse(content)
    default = gaz.by_name(DEFAULT_PLACE[src["key"]]) if src["key"] in DEFAULT_PLACE else None
    items = []
    for e in feed.entries:
        url = e.get("link")
        title = clean_title(e.get("title"))[:500]
        if not url or not title or is_junk_title(title):
            continue
        summary = clean(e.get("summary") or e.get("description"), 1200)
        places = gaz.find(f"{title}. {summary}")
        if not places and default:
            places = [default]
        lang = src.get("language") or detect_language(f"{title} {summary}")
        items.append(RawItem(
            url=url, title=title, summary=summary, language=lang, published_at=_when(e),
            outlet=src["name"], outlet_tier=src.get("tier", "B"), outlet_state=bool(src.get("state_media")),
            outlet_country=src.get("country"),
            places=places, meta={"tags": [t.get("term") for t in e.get("tags", []) if t.get("term")][:10]},
        ))
    return items


class RssCollector(Collector):
    key = "rss"
    interval_s = 600

    def __init__(self):
        self.sources = load_yaml("sources.yaml").get("rss") or []
        self.client = httpx.Client(timeout=25, follow_redirects=True,
                                   headers={"User-Agent": settings().http_user_agent})

    def _fetch(self, src: dict) -> tuple[dict, bytes | None, str | None]:
        try:
            r = self.client.get(src["url"])
            r.raise_for_status()
            return src, r.content, None
        except Exception as e:  # network errors are recorded per source, never fatal
            return src, None, f"{type(e).__name__}: {e}"[:300]

    def run(self, conn: psycopg.Connection) -> int:
        total = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(self._fetch, [s for s in self.sources if s.get("enabled", True)]))
        for src, content, err in results:
            sid = ensure_source(conn, f"rss:{src['key']}", src["name"], "rss", src["url"], src.get("country"),
                                src.get("language"), src.get("tier", "B"), bool(src.get("state_media")))
            if err:
                mark_polled(conn, sid, err)
                conn.commit()
                log.warning("rss %s: %s", src["key"], err)
                continue
            try:
                n = store_items(conn, sid, parse_feed(content, src))
                mark_polled(conn, sid)
                conn.commit()
                total += n
            except Exception as e:
                conn.rollback()
                mark_polled(conn, sid, f"parse: {e}"[:300])
                conn.commit()
                log.exception("rss %s parse failed", src["key"])
        return total
