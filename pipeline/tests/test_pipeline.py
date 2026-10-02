"""End to end: store items, cluster, triage, link, and read it all back through the API."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wassup.collectors.base import RawItem, ensure_source, store_items
from wassup.geo import gazetteer

pytestmark = pytest.mark.usefixtures("database")


def _items(now):
    g = gazetteer()
    kyiv, wash = g.by_name("Kyiv"), g.by_name("Washington")
    def it(n, title, places, ents=(), minutes=0, url=None, tier="B"):
        return RawItem(url=url or f"https://example.com/{n}", title=title, published_at=now - timedelta(minutes=minutes),
                       outlet=f"outlet{n}.com", outlet_tier=tier, places=places, entities=list(ents),
                       meta={"themes": ["ARMEDCONFLICT"]})
    return [
        it(1, "Russian missile strike on Kyiv kills civilians overnight", [kyiv], [("person", "Volodymyr Zelensky"), ("org", "Ukrainian Air Force")]),
        it(2, "Russian missile strike on Kyiv kills civilians, officials say", [kyiv], [("person", "Volodymyr Zelensky")], 5),
        it(3, "Russian missile strike on Kyiv kills civilians overnight, police say", [kyiv], [("org", "Ukrainian Air Force")], 9),
        it(4, "Senate votes on Ukraine air defense aid after Kyiv strike", [wash], [("person", "Volodymyr Zelensky"), ("org", "Ukrainian Air Force")], 20, tier="A"),
        it(5, "Premier League striker scores hat trick in derby win", [], minutes=30, url="https://example.com/sport/football/5"),
    ]


def test_end_to_end(database):
    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.signals import update_breaking, update_links
    from wassup.triage import run_triage

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        sid = ensure_source(conn, "test", "Test", "rss")
        assert store_items(conn, sid, _items(now)) == 5
        assert store_items(conn, sid, _items(now)) == 0  # duplicates by URL are ignored
        conn.commit()
        assert process_new(conn) == 5
        assert run_triage(conn) >= 3
        update_breaking(conn)
        update_links(conn)

        stories = conn.execute("SELECT id, title, item_count, desk, routed, excluded_reason FROM stories ORDER BY item_count DESC").fetchall()
        kyiv = stories[0]
        assert kyiv["item_count"] == 3 and kyiv["desk"] == "russia_ukraine" and kyiv["routed"]
        sport = next(s for s in stories if "Premier League" in s["title"])
        assert not sport["routed"] and sport["excluded_reason"] == "sports"

    client = TestClient(app)
    g = client.get("/api/globe", params={"hours": 6}).json()
    assert {s["id"] for s in g["stories"]} >= {kyiv["id"]}
    assert all(s["routed"] for s in g["stories"])
    assert any(p["name"] == "Kyiv" for p in g["places"])
    # The Senate story shares two actors with the strike story, so they are linked.
    assert any(l["kind"] == "same_actor" for l in g["links"])

    detail = client.get(f"/api/stories/{kyiv['id']}").json()
    assert len(detail["items"]) == 3 and detail["places"][0]["name"] == "Kyiv"
    assert detail["links"], "expected the Senate story to show up as connected"

    place_id = detail["places"][0]["id"]
    assert client.get(f"/api/places/{place_id}", params={"hours": 6}).json()["stories"][0]["id"] == kyiv["id"]

    graph = client.get(f"/api/graph/{kyiv['id']}").json()
    ids = {n["id"] for n in graph["nodes"]}
    assert graph["root"] in ids and all(e["source"] in ids and e["target"] in ids for e in graph["edges"])

    assert client.get("/api/stories", params={"hours": 6, "cold": True, "q": "Premier"}).json()[0]["title"].startswith("Premier")
    assert client.get("/api/timeline", params={"hours": 6, "buckets": 12}).json()["counts"]

    # Thumbs down moves the story to cold storage on the next triage pass.
    assert client.post(f"/api/stories/{kyiv['id']}/feedback", json={"value": -1}).json() == {"ok": True}
    with db.connect() as conn:
        run_triage(conn)
        assert conn.execute("SELECT routed, excluded_reason FROM stories WHERE id = %s", (kyiv["id"],)).fetchone() == {"routed": False, "excluded_reason": "you_dismissed"}
