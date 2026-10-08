"""Strikes from Telegram posts: finding the place, checking the model, grouping reports."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.tracks.strikes import STRIKE, _quote_ok, exact_spot, geocode, group_strikes, unorm


def test_unorm_handles_any_script():
    assert unorm("Pokrovs'k") == unorm("Pokrovsk") == "pokrovsk"
    assert unorm("Новосёлка") == unorm("Новоселка")
    assert unorm("Ivano-Frankivsk") == "ivano frankivsk"


def test_geocode_villages_in_any_spelling_and_refuses_to_guess():
    p = geocode("Pokrovsk", "Покровськ", "Donetsk Oblast", "Ukraine")
    assert p["country"] == "UA" and abs(p["lat"] - 48.28) < 0.05  # not the Pokrovsk in Siberia
    assert geocode("Kostiantynivka", "Костянтинівка", "Donetsk region", "Ukraine")["admin1"] == "Donetsk"
    assert geocode("Belgorod", "Белгород", "", "")["country"] == "RU"
    assert geocode("Nabatieh", "النبطية", "", "Lebanon")["country"] == "LB"
    assert geocode("Kharkiv", "Харкові", "", "")["admin1"] == "Kharkiv"  # the city beats villages of that name
    # Dozens of villages are called Novoselivka: without a province that tells them apart, no point.
    assert geocode("Novoselivka", None, "", "Ukraine") is None
    assert geocode("Atlantis", None, "", "") is None


def test_quote_must_come_from_the_post_and_name_the_place():
    post = "Вночі ворог атакував Харків ударними БпЛА. Влучання в обʼєкт енергетики у Харкові."
    assert _quote_ok("Влучання в обʼєкт енергетики у Харкові.", post, ["Kharkiv", "Харків"])
    assert not _quote_ok("Влучання в обʼєкт енергетики у Сумах.", post, ["Sumy", "Суми"])  # not in the post
    assert not _quote_ok("Вночі ворог атакував Харків ударними БпЛА.", post, ["Sumy", "Суми"])  # another place
    flight = "Черкащина - Реактивний БпЛА на/повз Жашків курсом на Вінниччину."
    assert not _quote_ok(flight, flight, ["Zhashkiv", "Жашків"])  # on its way, not a strike
    hit = "БпЛА курсом на Жашків, згодом влучання в обʼєкт у Жашкові."
    assert _quote_ok(hit, hit, ["Zhashkiv", "Жашків"])


def test_exact_spot_from_the_post():
    kyiv = (50.4547, 30.5238)
    assert exact_spot("Hit in Kyiv.\n📍 Coordinates: 50.4533461, 30.4356499", kyiv) == (50.4533461, 30.4356499)
    assert exact_spot("Coordinates: 48.28, 37.17 and 49.9818, 36.2548", kyiv) is None  # elsewhere: not this place
    assert exact_spot("Prices rose 3.5, 4.25 percent", kyiv) is None


def test_prefilter():
    assert STRIKE.search("Explosions heard in Odesa")
    assert STRIKE.search("Вибухи у Запоріжжі")
    assert STRIKE.search("غارة إسرائيلية على بلدة الخيام")
    assert not STRIKE.search("The minister met his counterpart to discuss trade")


def test_group_strikes_merges_reports_of_one_strike():
    t = datetime(2026, 10, 8, 3, tzinfo=timezone.utc)

    def r(i, lat, lon, hours, handle, lean, outcome="hit", news=0):
        return {"id": i, "observed_at": t + timedelta(hours=hours), "lat": lat, "lon": lon, "label": "Kharkiv",
                "status": "auto", "source_url": f"https://t.me/{handle}/{i}", "news": news,
                "props": {"handle": handle, "lean": lean, "weapon": "drone", "outcome": outcome, "quote": "q", "killed": i % 2}}

    events = group_strikes([r(1, 49.98, 36.25, 0, "a", "Ukraine", "explosions_heard"), r(2, 49.99, 36.27, 1, "b", "Ukraine"),
                            r(3, 50.0, 36.24, 2, "c", "Russia", news=3),
                            r(4, 49.98, 36.25, 12, "a", "Ukraine"),        # same place, half a day later: another strike
                            r(5, 46.48, 30.74, 1, "a", "Ukraine")], t + timedelta(hours=13))   # Odesa
    assert len(events) == 3
    first = next(e for e in events if e["ids"] == [1, 2, 3])
    assert first["channels"] == 3 and first["sides"] == ["Russia", "Ukraine"] and first["corroborated"]
    assert first["outcome"] == "hit" and first["news"] == 3 and first["killed"] == 1
    lone = next(e for e in events if e["ids"] == [5])
    assert not lone["corroborated"] and lone["channels"] == 1


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, schema):
        self.calls += 1
        if "Харків" in prompt:
            return {"strikes": [
                {"place": "Kharkiv", "place_original": "Харків", "province": "Kharkiv Oblast", "country": "Ukraine",
                 "date": None, "weapon": "drone", "attacker": "Russia", "outcome": "hit", "target": "energy facility",
                 "killed": None, "injured": 2, "quote": "Влучання в обʼєкт енергетики у Харкові."},
                {"place": "Sumy", "place_original": "Суми", "province": "Sumy Oblast", "country": "Ukraine",
                 "date": None, "weapon": "missile", "attacker": "Russia", "outcome": "hit", "target": "",
                 "killed": None, "injured": None, "quote": "Ракетний удар по Сумах."}]}  # not in the post: dropped
        if "Kharkiv" in prompt:
            return {"strikes": [
                {"place": "Kharkiv", "place_original": "Kharkiv", "province": "", "country": "Ukraine",
                 "date": None, "weapon": "drone", "attacker": "", "outcome": "hit", "target": "power plant",
                 "killed": None, "injured": None, "quote": "Drones hit a power plant in Kharkiv overnight."}]}
        return {"strikes": []}


@pytest.mark.usefixtures("database")
def test_strikes_from_posts_to_map():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.social.telegram import store_posts
    from wassup.tracks.strikes import run_strikes

    now = datetime.now(timezone.utc)
    posts = {
        "ua_news": "Вночі ворог атакував Харків ударними БпЛА. Влучання в обʼєкт енергетики у Харкові.",
        "osint_feed": "Drones hit a power plant in Kharkiv overnight. Footage shows a large fire.",
        "talk_show": "The minister met his counterpart in Ankara to discuss grain and trade routes.",
    }
    with db.connect() as conn:
        conn.execute("INSERT INTO strike_reads (item_id) SELECT id FROM items")  # posts from other tests
        for i, (handle, text) in enumerate(posts.items()):
            conn.execute("INSERT INTO social_accounts (platform, handle, status, added_by, lean) VALUES ('telegram', %s, 'following', 'seed', %s)",
                         (handle, "Ukraine" if handle == "ua_news" else None))
            acct = conn.execute("SELECT * FROM social_accounts WHERE handle = %s", (handle,)).fetchone()
            store_posts(conn, acct, {"name": handle, "subscribers": None, "posts": [
                {"id": 10 + i, "text": text, "at": now - timedelta(minutes=30 - i), "views": 1, "forwarded_from": None,
                 "forwarded_name": None, "links": [], "media": []}]})
        conn.commit()
        llm = FakeLLM()
        assert run_strikes(conn, llm=llm, max_seconds=60) == 2  # the talk show post is never asked about
        assert llm.calls == 2
        assert run_strikes(conn, llm=llm, max_seconds=60) == 0  # nothing is read twice
        rows = conn.execute("SELECT label, props FROM track_observations WHERE category = 'strike' ORDER BY id").fetchall()
        assert [r["label"] for r in rows] == ["Kharkiv", "Kharkiv"]
        track = conn.execute("SELECT id FROM tracks WHERE key = 'strikes-ukraine'").fetchone()["id"]

    client = TestClient(app)
    assert any(t["kind"] == "strikes" for t in client.get("/api/tracks").json())
    st = client.get(f"/api/tracks/{track}/state", params={"compare_days": 1}).json()
    assert st["kind"] == "strikes" and st["summary"]["strikes"] == 1 and st["summary"]["reports"] == 2
    ev = st["features"]["features"][0]["properties"]
    assert ev["channels"] == 2 and ev["corroborated"] and ev["weapon"] == "drone" and ev["attacker"] == "Russia"
    assert ev["target"] in ("energy facility", "power plant") and ev["injured"] == 2
    # Rejecting every report of a strike takes it off the map.
    for i in ev["ids"]:
        client.post(f"/api/tracks/observations/{i}", json={"status": "rejected"})
    assert client.get(f"/api/tracks/{track}/state", params={"compare_days": 1}).json()["summary"]["strikes"] == 0
