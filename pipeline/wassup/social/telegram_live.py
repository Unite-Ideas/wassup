"""Telegram through a logged in account: posts the moment they are published, channels whose web
page is turned off, and private channels the account has joined.

Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in .env (keys from my.telegram.org,
for a separate account, not your personal one), then log in once:

    docker compose exec app wassup telegram login

The login is kept in data/telegram, so it survives restarts. While Wassup runs:

- Followed channels are joined by the account, one a minute at most so Telegram does not take it
  for a spam bot, up to MAX_JOINED. Joined channels deliver new posts as they are published and
  are no longer fetched from their web page.
- Every 10 minutes each joined channel is asked for anything missed (a restart, a dropped
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
from datetime import timezone
from pathlib import Path

from .. import db
from ..config import settings

log = logging.getLogger(__name__)

MAX_JOINED = 450        # Telegram allows 500 channels per account; leave room for your own
JOIN_EVERY_S = 60
CATCH_UP_EVERY_S = 600
_TME = re.compile(r"https?://t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})", re.I)


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
                          flood_sleep_threshold=120)


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
            "media": ["telegram:photo"] if type(getattr(msg, "media", None)).__name__ == "MessageMediaPhoto" else []}


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
        last_join = last_catch = 0.0
        while self.client.is_connected():
            now = time.time()
            if now - last_join >= JOIN_EVERY_S:
                last_join = now
                await self._join_one()
            if now - last_catch >= CATCH_UP_EVERY_S:
                last_catch = now
                await self._sync_dialogs()
                await self._catch_up()
            await asyncio.sleep(5)

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
        """Join the next followed channel that is still read from its web page."""
        from telethon.errors import ChannelPrivateError, FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError
        from telethon.tl.functions.channels import JoinChannelRequest

        if len({v for v in self.by_tg.values()}) >= MAX_JOINED:
            return
        with db.connect() as conn:
            a = conn.execute(
                """SELECT id, handle FROM social_accounts WHERE platform = 'telegram' AND status = 'following' AND NOT banned
                   AND (via = 'web' OR tg_id IS NULL) AND handle NOT LIKE 'c/%%'
                   ORDER BY pinned DESC, score DESC NULLS LAST LIMIT 1""").fetchone()
        if not a:
            return
        try:
            ent = await self.client.get_entity(a["handle"])
            if getattr(ent, "megagroup", False) or not getattr(ent, "broadcast", False):
                raise ValueError("a group, not a channel")
            await self.client(JoinChannelRequest(ent))
        except FloodWaitError as e:
            log.warning("telegram asks to wait %ss before joining more channels", e.seconds)
            await asyncio.sleep(min(e.seconds, 3600))
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
        log.info("telegram: joined @%s", a["handle"])

    async def _catch_up(self) -> None:
        """Anything missed while away, for joined channels; and reading candidates with no public page."""
        from telethon.errors import FloodWaitError

        with db.connect() as conn:
            accts = conn.execute(
                """SELECT * FROM social_accounts WHERE platform = 'telegram' AND via = 'api' AND NOT banned
                   AND status IN ('following', 'candidate')
                   AND (status = 'following' OR last_checked_at IS NULL OR last_checked_at < now() - interval '2 hours')""").fetchall()
        for a in accts:
            try:
                target = a["tg_id"] if a["handle"].startswith("c/") else a["handle"]
                msgs = await self.client.get_messages(target, limit=50, min_id=a["last_post_id"] or 0)
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds, 600))
                continue
            except Exception as e:
                with db.connect() as conn:
                    conn.execute("UPDATE social_accounts SET last_error = %s, last_checked_at = now() WHERE id = %s",
                                 (f"{type(e).__name__}: {e}"[:300], a["id"]))
                    conn.commit()
                continue
            posts = [p for p in (to_post(m, self.usernames) for m in msgs if m and not getattr(m, "action", None)) if p]
            await asyncio.to_thread(self._store, a["id"], posts, getattr(msgs, "total", None))
            await asyncio.sleep(1)

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
