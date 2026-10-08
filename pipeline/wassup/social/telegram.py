"""Telegram public channels, read from their web previews (t.me/s/<channel>). No account, no
login: the same page anyone sees in a browser.

Every followed channel is checked every 15 minutes, candidates every two hours. New posts become
items like articles do, so they are grouped into stories, translated, triaged and placed on the
map. The post text is stored as the item's full text, so nothing needs fetching later. Which
channels a post forwards or links to is noted: that is how new channels are discovered.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from lxml import html as lh
from psycopg.types.json import Jsonb

from ..collectors.base import Collector, RawItem, ensure_source, store_items
from ..config import load_yaml, settings
from ..geo import gazetteer
from ..text import detect_language

log = logging.getLogger(__name__)

PAGE = "https://t.me/s/{handle}"
_HANDLE = re.compile(r"^https?://t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d+))?/?$")
_BG = re.compile(r"background-image:url\('([^']+)'\)")
NOT_CHANNELS = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "c", "s", "addlist", "boost", "contact"}


def cfg() -> dict:
    return load_yaml("social.yaml").get("telegram") or {}


def handle_of(url: str) -> str | None:
    m = _HANDLE.match((url or "").strip())
    if not m or m.group(1).lower() in NOT_CHANNELS or m.group(1).lower().endswith("bot"):
        return None  # bots are not channels
    return m.group(1).lower()


def _count(s: str | None) -> int | None:
    """'1.56M' -> 1560000."""
    if not s:
        return None
    s = s.strip().replace(" ", "")
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(s[-1:].upper(), 1)
    try:
        return int(float(s.rstrip("KMBkmb")) * mult)
    except ValueError:
        return None


def parse_page(page: str) -> dict:
    """The channel's name and subscriber count, and its posts, oldest first."""
    doc = lh.fromstring(page)
    title = doc.xpath('//meta[@property="og:title"]/@content')
    subs = None
    for c in doc.xpath('//div[contains(@class,"tgme_channel_info_counter")]'):
        if "subscriber" in " ".join(c.xpath('.//span[@class="counter_type"]/text()')):
            subs = _count("".join(c.xpath('.//span[@class="counter_value"]/text()')))
    posts = []
    for m in doc.xpath('//div[contains(@class,"tgme_widget_message ") and @data-post]'):
        handle, _, pid = m.get("data-post").partition("/")
        if not pid.isdigit():
            continue
        text_el = m.xpath('.//div[contains(@class,"tgme_widget_message_text")]')
        text = ""
        if text_el:
            el = text_el[-1]  # the last one is the post itself, not a quoted reply
            for br in el.xpath(".//br"):
                br.tail = "\n" + (br.tail or "")
            text = html.unescape(el.text_content()).strip()
        when = m.xpath('.//a[contains(@class,"tgme_widget_message_date")]//time/@datetime')
        if not when:
            continue
        fwd = m.xpath('.//a[contains(@class,"tgme_widget_message_forwarded_from_name")]/@href')
        fwd_name = m.xpath('.//*[contains(@class,"tgme_widget_message_forwarded_from_name")]//text()')
        links = {h for h in (handle_of(a) for a in (text_el[-1].xpath(".//a/@href") if text_el else [])) if h}
        handle = handle.lower()
        media = [mm.group(1) for st in m.xpath('.//a[contains(@class,"tgme_widget_message_photo_wrap")]/@style')
                 for mm in [_BG.search(st)] if mm][:6]
        posts.append({
            "id": int(pid), "handle": handle, "text": text,
            "at": datetime.fromisoformat(when[0].replace("Z", "+00:00")),
            "views": _count("".join(m.xpath('.//span[contains(@class,"tgme_widget_message_views")]/text()'))),
            "forwarded_from": handle_of(fwd[0]) if fwd else None,
            "forwarded_name": " ".join(" ".join(fwd_name).split()) or None,
            "links": sorted(links - {handle}),
            "media": media,
        })
    posts.sort(key=lambda p: p["id"])
    return {"name": html.unescape(title[0]) if title else None, "subscribers": subs, "posts": posts}


def _title(text: str) -> str:
    """A headline for a post: its first line, or its first sentence when that is long."""
    first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 8), text.strip())
    if len(first) > 240:
        cut = re.search(r"^(.{40,240}?[.!?])\s", first)
        first = cut.group(1) if cut else first[:237].rsplit(" ", 1)[0] + "..."
    return first


def sync_seeds(conn: psycopg.Connection) -> None:
    """Add seed channels that are not known yet. Channels already known keep their status, so a
    seed you banned or the scout dropped is not brought back."""
    for desk, seeds in (cfg().get("seeds") or {}).items():
        for s in seeds or []:
            conn.execute(
                """INSERT INTO social_accounts (platform, handle, status, added_by, desk, kind, lean)
                   VALUES ('telegram', %s, 'following', 'seed', %s, %s, %s)
                   ON CONFLICT (platform, handle) DO UPDATE SET kind = coalesce(social_accounts.kind, EXCLUDED.kind),
                     lean = coalesce(social_accounts.lean, EXCLUDED.lean), desk = coalesce(social_accounts.desk, EXCLUDED.desk)""",
                (s["handle"].lower(), desk, s.get("kind"), s.get("lean")))
    conn.commit()


class TelegramCollector(Collector):
    key = "telegram"
    interval_s = 60

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, follow_redirects=True,
                                             headers={"User-Agent": settings().http_user_agent, "Accept-Language": "en"})
        self._seeded = False
        self._lock = threading.Lock()

    def fetch(self, handle: str) -> dict:
        r = self.client.get(PAGE.format(handle=handle))
        r.raise_for_status()
        return parse_page(r.text)

    def run(self, conn: psycopg.Connection) -> int:
        if not self._seeded:
            sync_seeds(conn)
            self._seeded = True
        due = conn.execute(
            """SELECT * FROM social_accounts WHERE platform = 'telegram' AND status IN ('following', 'candidate')
               AND NOT banned AND via = 'web' AND next_check_at <= now()
               ORDER BY (status = 'following') DESC, next_check_at LIMIT 40""").fetchall()
        conn.commit()
        if not due:
            return 0
        with ThreadPoolExecutor(6) as ex:
            pages = list(ex.map(self._safe_fetch, [a["handle"] for a in due]))
        total = 0
        c = cfg()
        for acct, page in zip(due, pages):
            minutes = c.get("check_minutes", 15) if acct["status"] == "following" else c.get("candidate_check_minutes", 120)
            if isinstance(page, Exception):
                conn.execute("UPDATE social_accounts SET last_checked_at = now(), next_check_at = now() + %s * interval '1 minute', "
                             "last_error = %s WHERE id = %s", (minutes * 2, f"{type(page).__name__}: {page}"[:300], acct["id"]))
                conn.commit()
                continue
            if not page["posts"] and acct["last_checked_at"] is None:
                # No public page. With a logged in account it can still be read; without one a
                # candidate is dropped and a followed channel says why it shows nothing.
                from .telegram_live import live_available
                if live_available():
                    conn.execute("UPDATE social_accounts SET via = 'api', last_checked_at = now(), "
                                 "status_reason = coalesce(status_reason, 'No public page; read through the logged in account.') WHERE id = %s", (acct["id"],))
                elif acct["status"] == "candidate":
                    conn.execute("UPDATE social_accounts SET status = 'removed', status_reason = 'No public posts (private, empty or preview turned off).', "
                                 "status_changed_at = now(), last_checked_at = now() WHERE id = %s", (acct["id"],))
                else:
                    conn.execute("UPDATE social_accounts SET last_checked_at = now(), next_check_at = now() + interval '1 day', "
                                 "last_error = 'No public page. Log in a Telegram account (wassup telegram login) to read it.' WHERE id = %s", (acct["id"],))
                conn.commit()
                continue
            total += self._store(conn, acct, page)
            conn.execute(
                """UPDATE social_accounts SET name = coalesce(%s, name), subscribers = coalesce(%s, subscribers),
                          last_post_id = greatest(last_post_id, %s), last_checked_at = now(),
                          next_check_at = now() + %s * interval '1 minute', last_error = NULL WHERE id = %s""",
                (page["name"], page["subscribers"], max([p["id"] for p in page["posts"]] or [0]), minutes, acct["id"]))
            conn.commit()
        if total:
            log.info("telegram: %d new posts from %d channels", total, len(due))
        return total

    def _safe_fetch(self, handle: str):
        try:
            return self.fetch(handle)
        except Exception as e:  # one channel failing must not stop the rest
            return e

    def _store(self, conn: psycopg.Connection, acct: dict, page: dict) -> int:
        return store_posts(conn, acct, page)


def store_posts(conn: psycopg.Connection, acct: dict, page: dict) -> int:
    """Save a channel's new posts as items (shared by the web page reader and the logged in one)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    new = [p for p in page["posts"] if p["id"] > acct["last_post_id"] and p["at"] > cutoff]
    state = acct["kind"] == "state"
    src = ensure_source(conn, f"telegram:{acct['handle']}", page["name"] or acct["handle"], "telegram",
                        PAGE.format(handle=acct["handle"]), None, None, "S" if state else "U", state)
    if acct["source_id"] != src:
        conn.execute("UPDATE social_accounts SET source_id = %s WHERE id = %s", (src, acct["id"]))
    # Forwards and links are counted even for old posts: they are how channels are found.
    for p in page["posts"]:
        for h, kind in [(p["forwarded_from"], "forward")] + [(h, "link") for h in p["links"]]:
            if h and h != acct["handle"].lower():
                conn.execute(
                    """INSERT INTO social_mentions (platform, handle, from_account, kind) VALUES ('telegram', %s, %s, %s)
                       ON CONFLICT (platform, handle, from_account, kind) DO UPDATE SET n = social_mentions.n + 1, last_seen = now()""",
                    (h, acct["id"], kind))
    items, bodies, media = [], {}, {}
    g = gazetteer()
    for p in new:
        text = p["text"]
        if len(text) < 20:
            continue  # a photo or a sticker with no words
        url = f"https://t.me/{acct['handle']}/{p['id']}"
        items.append(RawItem(
            url=url, title=_title(text), summary=text[:1200], published_at=p["at"],
            language=detect_language(text), outlet=page["name"] or acct["handle"],
            outlet_tier="S" if state else "U", outlet_state=state, places=g.find(text),
            meta={"platform": "telegram", "handle": acct["handle"], "post_id": p["id"], "views": p["views"],
                  "forwarded_from": p["forwarded_from"] or p["forwarded_name"], "media": p["media"],
                  "kind": acct["kind"], "lean": acct["lean"], "candidate": acct["status"] == "candidate"}))
        bodies[url] = text
        media[url] = p["media"]
    if not items:
        return 0
    n = store_items(conn, src, items)
    # The post is the whole article: keep it as the full text so the reader does not fetch it.
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO item_texts (item_id, status, body, chars, images)
               SELECT id, 'ok', %s, %s, %s FROM items WHERE url = %s ON CONFLICT (item_id) DO NOTHING""",
            [(b, len(b), Jsonb([{"src": m, "caption": ""} for m in media[u]]), u) for u, b in bodies.items()])
    return n
