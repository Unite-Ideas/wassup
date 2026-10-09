"""Stories are listed under the places they are about, and on the desk they belong to."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.collectors.base import RawItem, weighted_places
from wassup.geo import Place


def test_outlet_country_is_not_a_story_location():
    # An Italian paper on an ICE shooting in New York: GDELT tags Italy (from the page), the
    # headline says New York. Italy is not what the story is about.
    italy = Place("gn:3175395", "Italy", "IT", "country", 42.8, 12.8)
    ny = Place("gn:5128581", "New York", "US", "city", 40.71, -74.0)
    it = RawItem(url="https://www.ansa.it/a", title="US: ICE agent shoots and injures man in New York",
                 published_at=datetime.now(timezone.utc), places=[italy, ny], meta={"feed": "translation"}, outlet_country="IT")
    w = weighted_places(it)
    assert max(x for p, _, x in w if p.name == "Italy") <= 0.15
    assert max(x for p, _, x in w if p.name == "United States") == 3.0
    # The same paper on Rome: its home country is named, and still gets the small boost.
    rome = Place("gn:3169070", "Rome", "IT", "city", 41.9, 12.5)
    it2 = RawItem(url="https://www.ansa.it/b", title="Strike closes schools in Rome", published_at=datetime.now(timezone.utc),
                  places=[rome, italy], meta={"feed": "translation"}, outlet_country="IT")
    assert max(x for p, _, x in weighted_places(it2) if p.name == "Rome") == 3.3


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        lines = [ln for ln in prompt.splitlines() if ln.startswith("[")]
        out = []
        for ln in lines:
            n = int(ln[1:ln.index("]")])
            if "steel plant" in ln:
                out.append({"n": n, "desk": "none", "country": "IT", "sure": True})
            elif "Sudan" in ln:
                out.append({"n": n, "desk": "world_watch", "country": "SD", "sure": True})
            else:
                out.append({"n": n, "desk": "migration", "country": "US", "sure": True})
        return {"stories": out}


@pytest.mark.usefixtures("database")
def test_desk_review_and_place_lists():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.collectors.base import ensure_source, store_items
    from wassup.triage import run_triage
    from wassup.triage.review import run_review

    now = datetime.now(timezone.utc)
    italy = Place("gn:3175395", "Italy", "IT", "country", 42.8, 12.8)
    ny = Place("gn:5128581", "New York", "US", "city", 40.71, -74.0)
    with db.connect() as conn:
        conn.execute("UPDATE stories SET routed = false")  # stories from other tests
        sid = ensure_source(conn, "class-test", "Class test", "rss")
        store_items(conn, sid, [
            RawItem(url="https://it.example/1", title="Strike at the Taranto steel plant over pay for migrants",
                    summary="Workers at the Taranto steel plant walked out.", published_at=now - timedelta(hours=2), places=[italy]),
            RawItem(url="https://it.example/2", title="Strike at the Taranto steel plant over pay for migrants, unions say",
                    summary="Unions at the Taranto steel plant in Italy.", published_at=now - timedelta(hours=1), places=[italy]),
            RawItem(url="https://us.example/1", title="ICE agent shoots migrant in New York during immigration raid",
                    summary="An immigration raid in New York.", published_at=now - timedelta(hours=2), places=[ny, italy],
                    meta={"feed": "translation"}, outlet_country="IT"),  # GDELT tagged Italy from an Italian page
            RawItem(url="https://us.example/2", title="ICE agent shoots migrant in New York during immigration raid, police say",
                    summary="Immigration agents in New York.", published_at=now - timedelta(hours=1), places=[ny]),
            RawItem(url="https://sd.example/1", title="Refugees flee fighting in Sudan as RSF advances on El Fasher",
                    summary="Thousands of refugees fled.", published_at=now - timedelta(hours=1), places=[]),
        ])
        conn.commit()
        process_new(conn)
        run_triage(conn)
        # The rules' mistake, as it happens with GDELT's topic tags: the strike on the Migration desk.
        conn.execute("UPDATE stories SET routed = true, desk = 'migration', excluded_reason = NULL WHERE title LIKE 'Strike at the Taranto%%'")
        conn.commit()
        llm = FakeLLM()
        assert run_review(conn, llm=llm) >= 3
        assert run_review(conn, llm=llm) == 0  # not twice until the story doubles
        rows = {r["title"][:20]: r for r in conn.execute(
            "SELECT coalesce(title_en, title) AS title, desk, routed, excluded_reason, desk_review FROM stories WHERE desk_review IS NOT NULL")}
        strike = rows["Strike at the Tarant"]
        assert not strike["routed"] and strike["excluded_reason"] == "desk_review_none"
        assert rows["Refugees flee fighti"]["desk"] == "world_watch"
        assert rows["ICE agent shoots mig"]["desk"] == "migration" and rows["ICE agent shoots mig"]["routed"]
        # Triage running again does not undo the review.
        conn.execute("UPDATE stories SET triaged_item_count = 0")
        conn.commit()
        run_triage(conn)
        assert not conn.execute("SELECT routed FROM stories WHERE title LIKE 'Strike at the Taranto%%'").fetchone()["routed"]
        italy_id = conn.execute("SELECT id FROM places WHERE key = 'gn:3175395'").fetchone()["id"]

    client = TestClient(app)
    place = client.get(f"/api/places/{italy_id}", params={"hours": 6, "cold": True}).json()
    by_title = {s["title"][:20]: s for s in place["stories"]}
    assert by_title["Strike at the Tarant"]["about"] is True
    assert by_title["ICE agent shoots mig"]["about"] is False  # mentions Italy, is about New York
    globe = client.get("/api/globe", params={"hours": 6, "cold": True}).json()
    it = next(p for p in globe["places"] if p["id"] == italy_id)
    assert it["story_count"] == 1  # the globe counts only stories about Italy
