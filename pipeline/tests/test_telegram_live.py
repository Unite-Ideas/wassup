"""The logged in Telegram reader: turning API messages into posts, and storing them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from wassup.social.telegram_live import to_post


class MessageEntityUrl(NS):
    pass


class MessageEntityTextUrl(NS):
    pass


class MessageMediaPhoto(NS):
    pass


def _msg(i, text, **kw):
    return NS(id=i, message=text, date=kw.get("date", datetime.now(timezone.utc)), views=kw.get("views", 1000),
              fwd_from=kw.get("fwd_from"), entities=kw.get("entities"), media=kw.get("media"),
              photo=kw.get("photo"), video=kw.get("video"))


def test_message_to_post():
    text = "Strikes on Odesa port overnight. Video: https://t.me/odesa_local/77 and https://t.me/SomeBot"
    m = _msg(5, text, fwd_from=NS(from_id=NS(channel_id=42), from_name=None),
             entities=[MessageEntityUrl(offset=text.index("https://t.me/odesa"), length=len("https://t.me/odesa_local/77")),
                       MessageEntityUrl(offset=text.index("https://t.me/Some"), length=len("https://t.me/SomeBot")),
                       MessageEntityTextUrl(offset=0, length=6, url="https://t.me/s/Front_News")],
             media=MessageMediaPhoto(), photo=NS(id=1))
    p = to_post(m, {42: "scoop_channel"})
    assert p["id"] == 5 and p["text"] == text and p["views"] == 1000
    assert p["forwarded_from"] == "scoop_channel"
    assert p["links"] == ["front_news", "odesa_local"]  # bots are not channels
    assert p["media"] == ["telegram:photo"]
    # A forward from a private channel has only a display name.
    q = to_post(_msg(6, "x" * 30, fwd_from=NS(from_id=None, from_name="Some Private Feed")), {})
    assert q["forwarded_from"] is None and q["forwarded_name"] == "Some Private Feed"


@pytest.mark.usefixtures("database")
def test_live_posts_are_stored_once_and_only_for_followed_channels(monkeypatch):
    from wassup import db
    from wassup.social.telegram_live import LiveReader

    with db.connect() as conn:
        a = conn.execute("""INSERT INTO social_accounts (platform, handle, name, status, added_by, via, tg_id)
                            VALUES ('telegram', 'c/777', 'Private Feed', 'following', 'you', 'api', 777) RETURNING id""").fetchone()["id"]
        b = conn.execute("""INSERT INTO social_accounts (platform, handle, name, status, added_by, via, tg_id)
                            VALUES ('telegram', 'paused_one', 'Paused', 'paused', 'seed', 'api', 778) RETURNING id""").fetchone()["id"]
        conn.commit()
    r = LiveReader()
    post = to_post(_msg(10, "Air raid alert across the Kyiv region, explosions heard"), {})
    r._store(a, [post], None)
    r._store(a, [post], None)  # the same post again (real time, then catch up): stored once
    r._store(b, [to_post(_msg(11, "A post from a paused channel, not wanted"), {})], None)
    with db.connect() as conn:
        rows = conn.execute("SELECT url FROM items WHERE url LIKE 'https://t.me/c/777/%%' OR url LIKE 'https://t.me/paused_one/%%'").fetchall()
        assert [r["url"] for r in rows] == ["https://t.me/c/777/10"]
        assert conn.execute("SELECT last_post_id FROM social_accounts WHERE id = %s", (a,)).fetchone()["last_post_id"] == 10


@pytest.mark.usefixtures("database")
def test_web_reader_hands_channels_without_a_page_to_the_account(monkeypatch):
    import httpx

    from wassup import db
    from wassup.social import telegram, telegram_live

    monkeypatch.setattr(telegram, "cfg", lambda: {"seeds": {}})
    monkeypatch.setattr(telegram_live, "live_available", lambda: True)
    empty = "<html><head><meta property='og:title' content='Hidden'></head><body></body></html>"
    with db.connect() as conn:
        conn.execute("INSERT INTO social_accounts (platform, handle, status, added_by) VALUES ('telegram', 'hidden_preview', 'candidate', 'discovered')")
        conn.commit()
        col = telegram.TelegramCollector(client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=empty))))
        col._seeded = True
        col.run(conn)
        row = conn.execute("SELECT status, via FROM social_accounts WHERE handle = 'hidden_preview'").fetchone()
        assert row["status"] == "candidate" and row["via"] == "api"


def test_join_pacing_ramps_up_and_respects_pauses():
    from wassup.social.telegram_live import LIVE_DEFAULTS, joins_allowed, may_join

    c = dict(LIVE_DEFAULTS)
    assert [joins_allowed(d, c) for d in (0, 1, 2, 4, 30)] == [8, 16, 24, 40, 40]
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    assert may_join({}, 0, c, now)  # first ever join
    st = {"since": "2026-10-08", "day": "2026-10-08", "count": 8}
    assert not may_join(st, 8, c, now)  # first day used up
    assert may_join(st, 8, c, now + timedelta(days=1))  # a new day, a bigger allowance
    assert not may_join({"since": "2026-10-01", "day": "2026-10-08", "count": 40}, 300, c, now)
    assert not may_join({"since": "2026-10-01"}, 450, c, now)  # channel limit
    paused = {"since": "2026-10-01", "paused_until": (now + timedelta(hours=3)).isoformat()}
    assert not may_join(paused, 10, c, now)
    assert may_join(paused, 10, c, now + timedelta(hours=4))
