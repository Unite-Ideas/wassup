"""The search bar: words in any order, accents and case ignored, close spellings, any article."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.search import words


def test_query_words():
    assert words("Marché des Enfants-Rouges") == ["marche", "enfants", "rouges"]
    assert words("the") == ["the"]  # a query of only small words still searches


@pytest.mark.usefixtures("database")
def test_search_finds_the_market_fire():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        sid = ensure_source(conn, "search-test", "Search test", "rss")
        store_items(conn, sid, [
            RawItem(url="https://f.example/1", title="Paris : un violent incendie ravage le Marché des Enfants-Rouges",
                    summary="Le plus vieux marché couvert de Paris a brûlé dans la nuit.", published_at=now - timedelta(hours=3)),
            # The story's own headline need not name it: a summary is enough.
            RawItem(url="https://f.example/2", title="Huge fire guts historic covered market in central Paris",
                    summary="Protesters are suspected of starting the fire at the Marche des Enfants Rouges.", published_at=now - timedelta(hours=2)),
            RawItem(url="https://f.example/3", title="Red Cross appeals for children in Sudan",
                    summary="Les enfants du Darfour.", published_at=now - timedelta(hours=2)),
        ])
        conn.commit()
        process_new(conn)
    client = TestClient(app)

    def titles(q):
        r = client.get("/api/search", params={"q": q}).json()
        return r, " ".join(m["title"] for m in r["matches"])

    r, t = titles("marche des enfants rouges")  # no accent
    assert "Enfants-Rouges" in t or "covered market" in t
    assert "Sudan" not in t  # every word must be there
    r, t = titles("rouges enfants marché")  # any order
    assert r["matches"] and not r["fuzzy"]
    r, t = titles("marche des entants rouges")  # a typo, as typed
    assert r["matches"] and r["fuzzy"] and r["matches"][0].get("close_spelling")
    assert "Sudan" not in t
    # The old search (the whole phrase in a story headline) found nothing for the same words.
    old = client.get("/api/stories", params={"q": "marche des entants rouges", "cold": True, "hours": 72}).json()
    assert old == []
