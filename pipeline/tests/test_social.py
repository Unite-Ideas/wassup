"""Telegram channels: reading posts, scoring channels, discovering and dropping them."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from wassup.social.scout import score
from wassup.social.telegram import handle_of, parse_page

PAGE = """<html><head><meta property="og:title" content="Front News"></head><body>
<div class="tgme_channel_info_counter"><span class="counter_value">1.2M</span> <span class="counter_type">subscribers</span></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="FrontNews/101">
  <div class="tgme_widget_message_forwarded_from accent_color">Forwarded from <a class="tgme_widget_message_forwarded_from_name" href="https://t.me/Scoop_Channel/55"><span>Scoop</span></a></div>
  <div class="tgme_widget_message_text js-message_text">Explosions reported in Kharkiv<br>Air defence active over the city. More: <a href="https://t.me/other_channel">here</a> and <a href="https://t.me/SomeNewsBot">bot</a></div>
  <a class="tgme_widget_message_photo_wrap" style="width:100px;background-image:url('https://cdn.example/p1.jpg')"></a>
  <span class="tgme_widget_message_views">12.5K</span>
  <a class="tgme_widget_message_date" href="https://t.me/FrontNews/101"><time datetime="2026-10-08T10:00:00+00:00">10:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="FrontNews/100">
  <div class="tgme_widget_message_text js-message_text">Earlier post</div>
  <a class="tgme_widget_message_date" href="https://t.me/FrontNews/100"><time datetime="2026-10-08T09:00:00+00:00">09:00</time></a>
</div></div></body></html>"""


def test_parse_page():
    d = parse_page(PAGE)
    assert d["name"] == "Front News" and d["subscribers"] == 1_200_000
    assert [p["id"] for p in d["posts"]] == [100, 101]
    p = d["posts"][1]
    assert p["text"].startswith("Explosions reported in Kharkiv\nAir defence")
    assert p["forwarded_from"] == "scoop_channel" and p["views"] == 12500 and p["media"] == ["https://cdn.example/p1.jpg"]
    assert p["links"] == ["other_channel"]  # bots are not channels


def test_handles():
    assert handle_of("https://t.me/DeepStateUA/123") == "deepstateua"
    assert handle_of("https://t.me/joinchat/abc") is None and handle_of("https://t.me/newsbot") is None


def test_score_rewards_early_corroborated_channels_and_smooths_small_samples():
    scoop = score({"posts": 60, "routed": 50, "forwarded": 2, "corroborated": 40, "evaluable": 50, "early": 25})
    echo = score({"posts": 60, "routed": 50, "forwarded": 40, "corroborated": 40, "evaluable": 50, "early": 1})
    noise = score({"posts": 60, "routed": 5, "forwarded": 0, "corroborated": 2, "evaluable": 55, "early": 0})
    lucky = score({"posts": 3, "routed": 3, "forwarded": 0, "corroborated": 3, "evaluable": 3, "early": 3})
    assert scoop > 60 > echo > noise and noise < 25
    assert lucky < scoop  # three good posts are not a track record


@pytest.mark.usefixtures("database")
def test_collector_discovery_and_scout(monkeypatch):
    from wassup import db
    from wassup.social import scout, telegram

    monkeypatch.setattr(telegram, "cfg", lambda: {"seeds": {"russia_ukraine": [{"handle": "FrontNews", "kind": "news"}, {"handle": "Second", "kind": "osint"}]}})
    now = datetime.now(timezone.utc)
    page = PAGE.replace("2026-10-08T10:00:00+00:00", now.isoformat()).replace("2026-10-08T09:00:00+00:00", (now - timedelta(hours=1)).isoformat())

    def handler(req):
        return httpx.Response(200, text=page.replace("FrontNews", req.url.path.rsplit("/", 1)[-1]))

    col = telegram.TelegramCollector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with db.connect() as conn:
        assert col.run(conn) == 2  # one post each from two channels; the 11 character one is skipped
        item = conn.execute("SELECT i.title, i.meta, t.body FROM items i JOIN item_texts t ON t.item_id = i.id WHERE i.url = 'https://t.me/frontnews/101'").fetchone()
        assert item["title"] == "Explosions reported in Kharkiv" and item["meta"]["forwarded_from"] == "scoop_channel"
        assert "Air defence" in item["body"]
        assert col.run(conn) == 0  # not due again yet
        # Both followed channels forward @scoop_channel and link @other_channel: candidates.
        assert scout.run_scout(conn) >= 2
        cands = {r["handle"]: r for r in conn.execute("SELECT handle, status, discovered_from FROM social_accounts WHERE added_by = 'discovered'")}
        assert set(cands) == {"scoop_channel", "other_channel"} and all(c["status"] == "candidate" for c in cands.values())
        # An old candidate that scores poorly is dropped; a banned channel is never rediscovered.
        conn.execute("UPDATE social_accounts SET created_at = now() - interval '30 days' WHERE handle = 'other_channel'")
        conn.execute("UPDATE social_accounts SET source_id = (SELECT source_id FROM social_accounts WHERE handle = 'frontnews') WHERE handle = 'other_channel'")
        conn.execute("UPDATE social_accounts SET banned = true, status = 'removed' WHERE handle = 'scoop_channel'")
        conn.commit()
        monkeypatch.setattr(scout, "_cfg", lambda: {**scout.DEFAULTS, "drop_below": 99})
        scout.run_scout(conn)
        assert conn.execute("SELECT status FROM social_accounts WHERE handle = 'other_channel'").fetchone()["status"] == "removed"
        assert conn.execute("SELECT banned FROM social_accounts WHERE handle = 'scoop_channel'").fetchone()["banned"]
