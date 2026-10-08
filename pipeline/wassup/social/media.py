"""Photos from Telegram posts that reported a strike, kept so the map can show them.

Only posts that put a strike on the map are fetched, so it stays small (a few hundred KB per
post). Photos are saved whole; for a video only its preview image is saved, never the video
itself, and no other kind of file is ever downloaded or opened.

- Posts read from a channel's web page carry the photo addresses; the strikes lane downloads
  them right after reading the post (the addresses stop working after a while).
- Posts that came through the logged in account are fetched by the live reader
  (social/telegram_live.py), which asks Telegram for the post and its album.

Files live in data/telegram/media/<channel>/<post>-<n>.jpg; the item_media table lists them.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
import psycopg

from ..config import settings

log = logging.getLogger(__name__)

MAX_PER_POST = 4
MAX_BYTES = 8_000_000
_SAFE = re.compile(r"[^a-z0-9_]+")


def media_dir() -> Path:
    d = settings().maps_dir.parent / "telegram" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_for(handle: str, post_id: int, n: int) -> Path:
    folder = media_dir() / (_SAFE.sub("_", handle.lower()) or "channel")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{post_id}-{n}.jpg"


def pending(conn: psycopg.Connection, via: str, limit: int = 20) -> list[dict]:
    """Strike posts from the last three days whose photos have not been looked for yet."""
    return conn.execute(
        """SELECT r.item_id, i.url, i.meta, a.handle, a.tg_id FROM strike_reads r
           JOIN items i ON i.id = r.item_id
           JOIN social_accounts a ON a.source_id = i.source_id AND a.platform = 'telegram'
           WHERE r.strikes > 0 AND r.media_status IS NULL AND a.via = %s AND r.read_at > now() - interval '3 days'
           ORDER BY r.read_at DESC LIMIT %s""", (via, limit)).fetchall()


def record(conn: psycopg.Connection, item_id: int, files: list[tuple[Path, str]], status: str) -> None:
    root = media_dir()
    for n, (path, kind) in enumerate(files):
        conn.execute(
            """INSERT INTO item_media (item_id, n, path, kind, bytes) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (item_id, n) DO NOTHING""",
            (item_id, n, str(path.relative_to(root)), kind, path.stat().st_size))
    conn.execute("UPDATE strike_reads SET media_status = %s WHERE item_id = %s", (status, item_id))


def fetch_web_media(conn: psycopg.Connection, client: httpx.Client | None = None) -> int:
    """Download the photos of strike posts read from public web pages."""
    rows = pending(conn, "web")
    if not rows:
        return 0
    client = client or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": settings().http_user_agent})
    got = 0
    for r in rows:
        meta = r["meta"] or {}
        urls = [u for u in meta.get("media") or [] if isinstance(u, str) and u.startswith("https://")][:MAX_PER_POST]
        files, status = [], "none"
        for n, url in enumerate(urls):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                if not resp.headers.get("content-type", "").startswith("image/") or len(resp.content) > MAX_BYTES:
                    continue
                path = file_for(r["handle"], meta.get("post_id") or r["item_id"], n)
                path.write_bytes(resp.content)
                files.append((path, "photo"))
            except Exception as e:
                log.debug("photo %s of item %s: %s", url, r["item_id"], e)
                status = "failed"
        record(conn, r["item_id"], files, "done" if files else status)
        conn.commit()
        got += len(files)
    if got:
        log.info("strikes: saved %d photos", got)
    return got


def photos_for(conn: psycopg.Connection, item_ids: list[int]) -> dict[int, list[str]]:
    """Addresses of the saved photos of these items, for the map."""
    out: dict[int, list[str]] = {}
    if not item_ids:
        return out
    for r in conn.execute("SELECT item_id, n FROM item_media WHERE item_id = ANY(%s) ORDER BY item_id, n", (item_ids,)):
        out.setdefault(r["item_id"], []).append(f"/api/media/{r['item_id']}/{r['n']}")
    return out
