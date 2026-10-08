"""Telegram through a logged in account: posts the moment they are published, channels whose web
page is turned off, and private channels the account has joined.

Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in .env (keys from my.telegram.org,
for a separate account, not your personal one), then log in once:

    docker compose exec app wassup telegram login

The login is kept in data/telegram, so it survives restarts. While Wassup runs:

- Followed channels are joined by the account, gently so Telegram does not take a new account
  for a spam bot: a few a day at first (pinned channels and seeds first), a few more each day
  after, never more than one every few minutes, and a full stop for a day if Telegram ever asks
  to slow down. The pace is set under telegram.live in config/social.yaml. Joined channels
  deliver new posts as they are published and are no longer fetched from their web page.
- Every 20 minutes each joined channel is asked for anything missed (a restart, a dropped
  connection), and candidates without a public page are read the same way, without joining.
- Channels you join yourself in the Telegram app on that account, private ones included, are
  picked up and followed, marked as added by you.
- Wassup only reads. It never posts, never opens files, and never joins groups (where members
  can see each other), only channels.

Without the keys or the login, none of this runs and the web page reader carries on alone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import db
from ..config import load_yaml, settings

log = logging.getLogger(__name__)

# Gentle by default. Telegram allows 500 channels per account; leave room for your own.
LIVE_DEFAULTS = {"max_joined": 450, "join_every_minutes": 6, "joins_first_day": 8, "joins_more_each_day": 8,
                 "max_joins_per_day": 40, "catch_up_minutes": 20, "catch_up_limit": 20,
                 "catch_up_pause_seconds": 3, "candidate_hours": 4}
_TME = re.compile(r"https?://t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})", re.I)


def live_cfg() -> dict:
    c = (load_yaml("social.yaml").get("telegram") or {}).get("live") or {}
    return {k: c.get(k, v) for k, v in LIVE_DEFAULTS.items()}


def joins_allowed(day_number: int, c: dict) -> int:
    """How many channels may be joined on the Nth day of reading (day 0 is the first)."""
    return min(c["max_joins_per_day"], c["joins_first_day"] + c["joins_more_each_day"] * max(0, day_number))


def may_join(st: dict, joined: int, c: dict, now: datetime) -> bool:
    """Within today's allowance, under the channel limit, and not told by Telegram to slow down.
    `st` is the join log kept in the kv table: first day, today's day and count, pause."""
    if joined >= c["max_joined"]:
        return False
    if st.get("paused_until") and datetime.fromisoformat(st["paused_until"]) > now:
        return False
    today = now.date()
    n = st.get("count", 0) if st.get("day") == today.isoformat() else 0
    since = date.fromisoformat(st.get("since") or today.isoformat())
    return n < joins_allowed((today - since).days, c)


def _creds() -> tuple[int, str, str] | None:
    api_id, api_hash, phone = (os.environ.get(k, "").strip() for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE"))
    if not (api_id.isdigit() and api_hash and phone):
        return None
    return int(api_id), api_hash, phone


def session_path() -> Path:
    d = settings().maps_dir.parent / "telegram"
    d.mkdir(parents=True, exist_ok=True)
    return d / "wassup"


def live_available() -> bool:
    """Keys are set and a login has been saved."""
    return _creds() is not None and Path(str(session_path()) + ".session").exists()


def _client():
    from telethon import TelegramClient

    api_id, api_hash, _ = _creds()
    return TelegramClient(str(session_path()), api_id, api_hash, device_model="Wassup", app_version="1.0",
                          flood_sleep_threshold=60)


def login() -> None:
    """Interactive, once: Telegram sends a code to the account, you type it here."""
    c = _creds()
    if c is None:
        print("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in .env first, then restart Wassup.")
        return
    client = _client()

    async def go():
        await client.start(phone=c[2])  # asks for the code, and the two step password if the account has one
        me = await client.get_me()
        print(f"Logged in as {me.first_name or ''} (@{me.username or 'no username'}). Wassup will start reading within a minute.")
        await client.disconnect()

    asyncio.run(go())


def to_post(msg, usernames: dict[int, str]) -> dict | None:
    """A Telethon message as the post dict the web reader produces."""
    text = msg.message or ""
    fwd = None
    fh = getattr(msg, "fwd_from", None)
    if fh is not None:
        cid = getattr(getattr(fh, "from_id", None), "channel_id", None)
        fwd = usernames.get(cid) if cid else None
        fwd = fwd or (fh.from_name or None)
    links = set()
    for e in msg.entities or []:
        url = getattr(e, "url", None) or (text[e.offset:e.offset + e.length] if type(e).__name__ == "MessageEntityUrl" else None)
        m = _TME.match(url or "")
        if m:
            links.add(m.group(1).lower())
    return {"id": msg.id, "text": text, "at": msg.date.astimezone(timezone.utc), "views": getattr(msg, "views", None),
            "forwarded_from": fwd.lower() if fwd and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", fwd) else None,
            "forwarded_name": fwd, "links": sorted(l for l in links if not l.endswith("bot")),
            "media": ["telegram:photo"] if getattr(msg, "photo", None) else ["telegram:video"] if getattr(msg, "video", None) else []}


class LiveReader:
    def __init__(self):
        self.client = None
        self.usernames: dict[int, str] = {}   # channel id -> username, for naming forwards
        self.by_tg: dict[int, int] = {}       # channel id -> social_accounts.id

    def start(self) -> None:
        threading.Thread(target=self._run, name="telegram-live", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                asyncio.run(self._main())
            except Exception:
                log.exception("telegram live reader stopped; restarting in a minute")
            time.sleep(60)

    async def _main(self) -> None:
        from telethon import events

        self.client = _client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("Telegram keys are set but the account is not logged in: run `wassup telegram login`")
            await self.client.disconnect()
            await asyncio.sleep(3600)
            return
        me = await self.client.get_me()
        log.info("telegram: reading as @%s", me.username or me.id)

        @self.client.on(events.NewMessage())
        async def on_message(ev):
            if not ev.is_channel or ev.is_group:
                return
            acct_id = self.by_tg.get(ev.chat_id)
            if acct_id:
                await asyncio.to_thread(self._store, acct_id, [to_post(ev.message, self.usernames)], None)

        await self._sync_dialogs()
        last_join = time.time()  # the first join waits a full interval after a (re)start
        last_catch = last_media = 0.0
        while self.client.is_connected():
            c = live_cfg()
            now = time.time()
            if now - last_join >= c["join_every_minutes"] * 60:
                last_join = now
                if may_join(self._join_log(), len(set(self.by_tg.values())), c, datetime.now(timezone.utc)):
                    await self._join_one()
            if now - last_media >= 60:
                last_media = now
                await self._fetch_media()
            if now - last_catch >= c["catch_up_minutes"] * 60:
                last_catch = now
                await self._sync_dialogs()
                await self._catch_up(c)
            await asyncio.sleep(5)

    # --- pacing ------------------------------------------------------------------------------

    def _join_log(self, joined: bool = False, pause_until: datetime | None = None) -> dict:
        """Read the join log, noting a join or a pause first if asked."""
        with db.connect() as conn:
            st = db.kv_get(conn, "telegram_joins") or {}
            today = datetime.now(timezone.utc).date().isoformat()
            changed = not st.get("since")
            st.setdefault("since", today)
            if joined:
                st["count"] = (st.get("count", 0) if st.get("day") == today else 0) + 1
                st["day"] = today
                changed = True
            if pause_until is not None:
                st["paused_until"] = pause_until.isoformat()
                changed = True
            if changed:
                db.kv_set(conn, "telegram_joins", st)
                conn.commit()
        return st

    # --- bookkeeping -------------------------------------------------------------------------

    async def _sync_dialogs(self) -> None:
        """Channels the account is in: link them to accounts, and follow ones you joined yourself."""
        from telethon.tl.types import Channel

        async for d in self.client.iter_dialogs():
            ent = d.entity
            if not isinstance(ent, Channel) or ent.megagroup:  # channels only, never groups
                continue
            handle = (ent.username or f"c/{ent.id}").lower()
            if ent.username:
                self.usernames[ent.id] = ent.username.lower()
            with db.connect() as conn:
                row = conn.execute(
                    """INSERT INTO social_accounts (platform, handle, name, status, added_by, via, tg_id, status_reason)
                       VALUES ('telegram', %s, %s, 'following', 'you', 'api', %s, 'Joined on the Telegram account.')
                       ON CONFLICT (platform, handle) DO UPDATE SET via = 'api', tg_id = EXCLUDED.tg_id,
                         name = coalesce(social_accounts.name, EXCLUDED.name)
                       RETURNING id, banned""", (handle, ent.title, ent.id)).fetchone()
                conn.commit()
            if not row["banned"]:
                self.by_tg[ent.id] = row["id"]
                self.by_tg[int(f"-100{ent.id}")] = row["id"]

    async def _join_one(self) -> None:
        """Join the next followed channel that is still read from its web page: pinned channels
        first, then seeds, then the best scored."""
        from telethon.errors import ChannelPrivateError, FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
        from telethon.tl.functions.channels import JoinChannelRequest

        with db.connect() as conn:
            a = conn.execute(
                """SELECT id, handle FROM social_accounts WHERE platform = 'telegram' AND status = 'following' AND NOT banned
                   AND (via = 'web' OR tg_id IS NULL) AND handle NOT LIKE 'c/%%'
                   ORDER BY pinned DESC, (added_by = 'seed') DESC, score DESC NULLS LAST LIMIT 1""").fetchone()
        if not a:
            return
        try:
            ent = await self.client.get_entity(a["handle"])
            if getattr(ent, "megagroup", False) or not getattr(ent, "broadcast", False):
                raise ValueError("a group, not a channel")
            await self.client(JoinChannelRequest(ent))
        except FloodWaitError as e:
            # Telegram says slow down: no joins for twice what it asks, and at least a day.
            until = datetime.now(timezone.utc) + timedelta(seconds=max(2 * e.seconds, 86400))
            self._join_log(pause_until=until)
            log.warning("telegram asked to wait %ss before joining more; no joins until %s",
                        e.seconds, until.strftime("%Y-%m-%d %H:%M UTC"))
            return
        except (ValueError, UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError) as e:
            with db.connect() as conn:
                conn.execute("UPDATE social_accounts SET via = 'web', tg_id = -1, last_error = %s WHERE id = %s",
                             (f"Cannot join on Telegram: {e}"[:300], a["id"]))
                conn.commit()
            return
        self.usernames[ent.id] = a["handle"]
        self.by_tg[ent.id] = a["id"]
        self.by_tg[int(f"-100{ent.id}")] = a["id"]
        with db.connect() as conn:
            conn.execute("UPDATE social_accounts SET via = 'api', tg_id = %s, name = coalesce(name, %s) WHERE id = %s",
                         (ent.id, ent.title, a["id"]))
            conn.commit()
        n = self._join_log(joined=True).get("count")
        log.info("telegram: joined @%s (%s today)", a["handle"], n)

    async def _catch_up(self, c: dict) -> None:
        """Anything missed while away, for joined channels; and reading candidates with no public page."""
        from telethon.errors import FloodWaitError

        with db.connect() as conn:
            accts = conn.execute(
                """SELECT * FROM social_accounts WHERE platform = 'telegram' AND via = 'api' AND NOT banned
                   AND status IN ('following', 'candidate')
                   AND (status = 'following' OR last_checked_at IS NULL OR last_checked_at < now() - %s * interval '1 hour')""",
                (c["candidate_hours"],)).fetchall()
        for a in accts:
            try:
                target = a["tg_id"] if a["handle"].startswith("c/") else a["handle"]
                msgs = await self.client.get_messages(target, limit=c["catch_up_limit"], min_id=a["last_post_id"] or 0)
            except FloodWaitError as e:
                log.warning("telegram asked to wait %ss while catching up; the rest waits for the next round", e.seconds)
                await asyncio.sleep(min(e.seconds + 30, 900))
                return
            except Exception as e:
                with db.connect() as conn:
                    conn.execute("UPDATE social_accounts SET last_error = %s, last_checked_at = now() WHERE id = %s",
                                 (f"{type(e).__name__}: {e}"[:300], a["id"]))
                    conn.commit()
                continue
            posts = [p for p in (to_post(m, self.usernames) for m in msgs if m and not getattr(m, "action", None)) if p]
            await asyncio.to_thread(self._store, a["id"], posts, getattr(msgs, "total", None))
            await asyncio.sleep(c["catch_up_pause_seconds"])

    async def _fetch_media(self) -> None:
        """Photos (and video previews) of posts that put a strike on the map. Nothing else is
        downloaded: no videos, no documents."""
        from telethon.errors import FloodWaitError

        from .media import MAX_PER_POST, file_for, pending, record

        with db.connect() as conn:
            rows = pending(conn, "api", limit=10)
        for r in rows:
            post_id = (r["meta"] or {}).get("post_id")
            files, status = [], "none"
            if not post_id:
                with db.connect() as conn:
                    record(conn, r["item_id"], [], "none")
                    conn.commit()
                continue
            try:
                target = r["tg_id"] if r["handle"].startswith("c/") else r["handle"]
                # An album is several messages sharing a grouped_id, the post being the first.
                msgs = [m for m in await self.client.get_messages(target, ids=list(range(post_id, post_id + 10))) if m]
                first = next((m for m in msgs if m.id == post_id), None)
                if first is not None:
                    album = [m for m in msgs if first.grouped_id and m.grouped_id == first.grouped_id] or [first]
                    for m in album:
                        if len(files) >= MAX_PER_POST:
                            break
                        path = file_for(r["handle"], post_id, len(files))
                        if m.photo:
                            saved = await self.client.download_media(m, file=str(path))
                            kind = "photo"
                        elif m.video and m.video.thumbs:
                            saved = await self.client.download_media(m, file=str(path), thumb=-1)
                            kind = "video_preview"
                        else:
                            continue
                        if saved:
                            files.append((path, kind))
                status = "done" if files else "none"
            except FloodWaitError as e:
                log.warning("telegram asked to wait %ss while fetching photos", e.seconds)
                return
            except Exception as e:
                log.debug("photos of item %s: %s", r["item_id"], e)
                status = "failed"
            with db.connect() as conn:
                record(conn, r["item_id"], files, status)
                conn.commit()
            if files:
                log.info("telegram: saved %d photos for a strike from @%s", len(files), r["handle"])
            await asyncio.sleep(2)

    def _store(self, acct_id: int, posts: list[dict], subscribers) -> None:
        from .telegram import store_posts

        with db.connect() as conn:
            acct = conn.execute("SELECT * FROM social_accounts WHERE id = %s", (acct_id,)).fetchone()
            if not acct or acct["banned"] or acct["status"] not in ("following", "candidate"):
                return
            page = {"name": acct["name"], "subscribers": None, "posts": sorted(posts, key=lambda p: p["id"])}
            n = store_posts(conn, acct, page)
            conn.execute("UPDATE social_accounts SET last_post_id = greatest(last_post_id, %s), last_checked_at = now(), "
                         "last_error = NULL WHERE id = %s", (max([p["id"] for p in posts] or [0]), acct_id))
            conn.commit()
        if n:
            log.debug("telegram live: %d posts from @%s", n, acct["handle"])


def start_live() -> bool:
    """Start reading as soon as there is a login: now, or within a minute of `wassup telegram login`."""
    if _creds() is None:
        return False

    def wait_then_run():
        warned = False
        while not live_available():
            if not warned:
                log.warning("Telegram keys are set but there is no login yet: run `docker compose exec app wassup telegram login`")
                warned = True
            time.sleep(60)
        LiveReader()._run()

    threading.Thread(target=wait_then_run, name="telegram-live", daemon=True).start()
    return True
