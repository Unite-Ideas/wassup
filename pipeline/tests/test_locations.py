from datetime import datetime, timezone

import pytest

from wassup.collectors.base import RawItem, weighted_places
from wassup.geo import Place, country_of_domain, gazetteer


def test_plural_demonyms_and_domains():
    g = gazetteer()
    assert [p.country for p in g.find("Why Canadians are paying more at the pump")] == ["CA"]
    assert "IQ" in {p.country for p in g.find("Iraqis vote in local elections")}
    assert country_of_domain("https://www.cbc.ca/news/1") == "CA"
    assert country_of_domain("bbc.co.uk") == "GB"
    assert country_of_domain("reuters.com") is None


def test_caption_place_contradicting_headline_is_ignored():
    wellington = Place("gn:2179537", "Wellington", "NZ", "city", -41.28, 174.77)
    london_on = Place("gdelt:london-on", "London", "CA", "city", 42.98, -81.25)
    it = RawItem(url="https://lfpress.com/gas", title="Gas tax: Why Canadians are paying more at the pump in some provinces",
                 published_at=datetime.now(timezone.utc), places=[london_on, wellington], meta={"feed": "english"})
    w = {p.name: weight for p, _, weight in weighted_places(it)}
    assert w["Canada"] == 3.0          # added from the headline
    assert w["Wellington"] <= 0.15     # contradicts the headline, nothing backs it
    assert w["London"] > 0.5           # same country as the headline, early in the article


def test_distrusted_place_down_weighted_without_support():
    p = Place("gn:1", "Somewhere", "NZ", "city", 0, 0)
    it = RawItem(url="https://x.com/a", title="Markets rally on strong earnings", published_at=datetime.now(timezone.utc),
                 places=[p], meta={"feed": "english"})
    assert weighted_places(it)[0][2] == 1.0
    assert weighted_places(it, {"gn:1"})[0][2] == pytest.approx(0.2)


@pytest.mark.usefixtures("database")
def test_story_location_check_and_your_fix():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.collectors.base import ensure_source, store_items
    from wassup.locate import run_locate
    from wassup.triage import run_triage

    g = gazetteer()
    kyiv = g.by_name("Kyiv")
    caption = Place("gn:2179537", "Wellington", "NZ", "city", -41.28, 174.77)
    with db.connect() as conn:
        sid = ensure_source(conn, "loc_test", "Loc test", "rss")
        store_items(conn, sid, [RawItem(url=f"https://loc.example/{i}", title=f"Missile strikes on energy grid deepen blackouts as war drags on, officials say {i}",
                                        published_at=datetime.now(timezone.utc), places=[caption, kyiv], meta={"feed": "english", "themes": ["ARMEDCONFLICT"]},
                                        outlet=f"o{i}.com") for i in range(3)])
        conn.commit()
        process_new(conn)
        run_triage(conn)
        story = conn.execute("SELECT s.* FROM stories s JOIN items i ON i.story_id = s.id WHERE i.url = 'https://loc.example/0'").fetchone()
        assert story["routed"] and story["location_source"] == "tagger"  # nothing in the headline names a place

        class Picker:
            name = "jev"
            def available(self): return True
            def choose(self, ctx, options):
                return next(k for k, v in options.items() if v.startswith("Ukraine")), 0.9

        assert run_locate(conn, locators=[Picker()]) >= 1
        after = conn.execute("SELECT lat, location_source, location_locked FROM stories WHERE id = %s", (story["id"],)).fetchone()
        assert after["location_source"] == "jev" and after["location_locked"] and after["lat"] > 40

    c = TestClient(app)
    hits = c.get("/api/search/places", params={"q": "Lviv"}).json()
    assert hits and hits[0]["name"] == "Lviv"
    assert c.post(f"/api/stories/{story['id']}/location", json={"place_key": hits[0]["key"]}).json() == {"ok": True}
    d = c.get(f"/api/stories/{story['id']}").json()
    assert d["location_source"] == "you" and d["places"][0]["name"] == "Lviv" and d["places"][0]["is_primary"]
    assert c.post(f"/api/stories/{story['id']}/location", json={"off_map": True}).json() == {"ok": True}
    with db.connect() as conn:
        s2 = conn.execute("SELECT lat FROM stories WHERE id = %s", (story["id"],)).fetchone()
        assert s2["lat"] is None
        assert conn.execute("SELECT count(*) n FROM location_corrections WHERE story_id = %s", (story["id"],)).fetchone()["n"] == 2
        assert conn.execute("SELECT false_positive_count FROM places WHERE key = %s", (hits[0]["key"],)).fetchone()["false_positive_count"] == 1
