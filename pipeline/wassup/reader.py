"""Reads the full text of articles, not just their headlines.

For articles in tracked stories (or every article, with READ_SCOPE=all), Wassup downloads the
page, pulls out the article text with trafilatura, and keeps it with the lead image and photo
captions. Desks then brief from the articles themselves, and the story panel can show them.

Downloads run many at a time (READ_CONCURRENCY, default 32) but never more than two at once
per website, and a site that keeps failing is left alone for a few hours. Pulling text out of
HTML is CPU work, so it runs in a pool of processes, one per core up to READ_WORKERS (default
16). Paywalled or blocked pages are recorded as such and not tried again.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

MAX_CHARS = 30_000
MIN_CHARS = 300          # shorter than this is a teaser, a cookie wall or a paywall
MAX_AGE_HOURS = 48       # older articles are not fetched any more
PER_HOST = 2


def _scope() -> str:
    return os.environ.get("READ_SCOPE", "tracked")


PENDING = """
    SELECT i.id, i.url FROM items i JOIN stories s ON s.id = i.story_id
    WHERE i.published_at > now() - %(age)s * interval '1 hour'
      AND (%(all)s OR s.routed)
      AND NOT EXISTS (SELECT 1 FROM item_texts t WHERE t.item_id = i.id)
    ORDER BY s.routed DESC, s.significance DESC, i.published_at DESC
    LIMIT %(n)s"""


def extract(url: str, html: str) -> dict:
    """Article text, lead image and photo captions from a page. Runs in a worker process."""
    import trafilatura
    from lxml import html as lh

    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False, favor_precision=True) or ""
    images, lead = [], None
    try:
        doc = lh.fromstring(html)
        og = doc.xpath('//meta[@property="og:image"]/@content | //meta[@name="twitter:image"]/@content')
        lead = urljoin(url, og[0].strip()) if og else None
        for fig in doc.xpath("//figure")[:12]:
            src = fig.xpath(".//img/@src | .//img/@data-src")
            cap = " ".join(" ".join(fig.xpath(".//figcaption//text()")).split())
            if src and cap:
                images.append({"src": urljoin(url, src[0].strip()), "caption": cap[:500]})
            if len(images) >= 6:
                break
    except Exception:
        pass
    return {"text": text[:MAX_CHARS], "lead_image": lead, "images": images}


class Reader:
    def __init__(self, client: httpx.Client | None = None):
        self.concurrency = int(os.environ.get("READ_CONCURRENCY", "32"))
        workers = int(os.environ.get("READ_WORKERS", str(min(16, os.cpu_count() or 4))))
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15, connect=8), follow_redirects=True, http2=False,
            limits=httpx.Limits(max_connections=self.concurrency * 2, max_keepalive_connections=self.concurrency),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/128.0 Safari/537.36", "Accept-Language": "en;q=0.9,*;q=0.5"})
        self.pool = ProcessPoolExecutor(max_workers=workers)
        self.hosts: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(PER_HOST))
        self.failures: dict[str, int] = defaultdict(int)
        self.resting: dict[str, float] = {}

    def _fetch(self, item: dict) -> dict:
        host = urlsplit(item["url"]).hostname or ""
        if self.resting.get(host, 0) > time.time():
            return {"id": item["id"], "status": "skipped", "http": None}
        with self.hosts[host]:
            try:
                r = self.client.get(item["url"])
            except httpx.HTTPError as e:
                self._failed(host)
                return {"id": item["id"], "status": "failed", "http": None, "error": type(e).__name__}
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            self._failed(host)
            status = "blocked" if r.status_code in (401, 402, 403, 429, 451) else "failed"
            return {"id": item["id"], "status": status, "http": r.status_code}
        self.failures[host] = 0
        return {"id": item["id"], "status": "fetched", "http": 200, "url": str(r.url), "html": r.text}

    def _failed(self, host: str) -> None:
        self.failures[host] += 1
        if self.failures[host] >= 5:  # a site that keeps refusing is left alone for six hours
            self.resting[host] = time.time() + 6 * 3600
            self.failures[host] = 0

    def run(self, conn: psycopg.Connection, max_seconds: float = 60) -> int:
        started, done = time.time(), 0
        while time.time() - started < max_seconds:
            items = conn.execute(PENDING, {"age": MAX_AGE_HOURS, "all": _scope() == "all", "n": self.concurrency * 4}).fetchall()
            conn.commit()
            if not items:
                break
            with ThreadPoolExecutor(self.concurrency) as ex:
                fetched = list(ex.map(self._fetch, items))
            pages = [f for f in fetched if f["status"] == "fetched"]
            parsed = dict(zip((p["id"] for p in pages),
                              self.pool.map(extract, [p["url"] for p in pages], [p["html"] for p in pages], chunksize=4)))
            rows = []
            for f in fetched:
                if f["status"] == "fetched":
                    x = parsed[f["id"]]
                    ok = len(x["text"]) >= MIN_CHARS
                    rows.append((f["id"], "ok" if ok else "short", f["http"], x["text"] if ok else None, len(x["text"]),
                                 x["lead_image"], Jsonb(x["images"])))
                else:
                    rows.append((f["id"], f["status"], f["http"], None, 0, None, Jsonb([])))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO item_texts (item_id, status, http_status, body, chars, lead_image, images)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (item_id) DO NOTHING""", rows)
            conn.commit()
            done += len(rows)
        if done:
            ok = conn.execute("SELECT count(*) n FROM item_texts WHERE fetched_at > now() - interval '2 minutes' AND status = 'ok'").fetchone()["n"]
            log.info("read %d articles (%d with full text in the last 2 minutes)", done, ok)
        return done


_reader: Reader | None = None


def run_reader(conn: psycopg.Connection) -> int:
    global _reader
    if os.environ.get("READ_SCOPE", "tracked") == "off":
        return 0
    if _reader is None:
        _reader = Reader()
    return _reader.run(conn)
