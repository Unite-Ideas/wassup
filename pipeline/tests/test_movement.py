"""Movement tracks: following a migrant caravan from news reports."""
from datetime import date, datetime, timedelta, timezone

import pytest

from wassup.tracks.movement import daily_path, is_caravan_text


def test_caravan_means_migrants():
    assert is_caravan_text("Nueva caravana de migrantes sale de Tapachula")
    assert is_caravan_text("Migrant caravan heads north", "Some 1,500 migrants left the border city")
    assert not is_caravan_text("Caravan holidays: the best parks in Devon")
    assert not is_caravan_text("Trump's campaign caravan rolls through Ohio")


def _p(i, d, lat, lon, status="auto", conf=0.8):
    return {"id": i, "day": d, "lat": lat, "lon": lon, "status": status, "confidence": conf}


def test_daily_path_takes_the_agreed_place_and_drops_impossible_jumps():
    d = date(2026, 10, 1)
    pts = [
        _p(1, d, 14.905, -92.259),                         # Tapachula
        _p(2, d, 14.92, -92.38),                           # Alvaro Obregon, next door: agrees
        _p(3, d + timedelta(1), 15.139, -92.463),          # Huixtla
        _p(4, d + timedelta(1), 15.14, -92.47),            # Huixtla again
        _p(5, d + timedelta(1), 19.825, -101.039),         # the wrong Alvaro Obregon, 900 km away: a minority
        _p(6, d + timedelta(2), 19.43, -99.13),            # "in Mexico City" a day later: impossible
        _p(7, d + timedelta(3), 15.434, -92.900),          # Mapastepec
    ]
    path, outliers = daily_path(pts)
    assert [p["id"] for p in path] in ([1, 3, 7], [2, 3, 7], [1, 4, 7], [2, 4, 7])
    assert outliers == {5, 6}


def test_a_confirmed_report_wins_its_day():
    d = date(2026, 10, 1)
    path, _ = daily_path([_p(1, d, 14.905, -92.259), _p(2, d, 14.906, -92.26), _p(3, d, 15.43, -92.9, status="confirmed", conf=0.5)])
    assert [p["id"] for p in path] == [3]


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, schema):
        self.calls += 1
        if "Huixtla" in prompt:
            return {"about_group": True, "reports": [
                {"place": "Huixtla", "country": "Mexico", "date": "2026-10-03", "status": "arrived", "people": 1200,
                 "quote": "Caravana migrante llega a Huixtla", "lat": None, "lon": None},
                {"place": "Huehuetán", "country": "Mexico", "date": "2026-10-03", "status": "stopped", "people": None,
                 "quote": "Caravana migrante llega a Huixtla", "lat": None, "lon": None},  # the quote is about Huixtla
                {"place": "Escuintla", "country": "Mexico", "date": "2026-10-03", "status": "at", "people": None,
                 "quote": "The caravan rested in Escuintla before dawn", "lat": None, "lon": None},  # not in the article
                {"place": "Ciudad de México", "country": "Mexico", "date": None, "status": "heading_to", "people": None,
                 "quote": "con destino a la Ciudad de México", "lat": None, "lon": None}]}
        return {"about_group": True, "reports": [
            {"place": "Tapachula", "country": "Mexico", "date": "2026-10-02", "status": "departed", "people": 1500,
             "quote": "Unos 1.500 migrantes salieron de Tapachula el jueves.", "lat": None, "lon": None},
            {"place": "Álvaro Obregón", "country": "Mexico", "date": "2026-10-02", "status": "stopped", "people": None,
             "quote": "Tras caminar 20 kilómetros, el grupo pernoctó en Álvaro Obregón.", "lat": None, "lon": None}]}


@pytest.mark.usefixtures("database")
def test_caravan_track_from_articles_to_map():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.tracks.movement import run_movements

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        sid = ensure_source(conn, "caravan-test", "Caravan test", "rss")
        store_items(conn, sid, [
            RawItem(url="https://mx.example/1", title="Nueva caravana de migrantes sale de Tapachula rumbo al norte", published_at=now - timedelta(hours=30)),
            RawItem(url="https://mx.example/2", title="Nueva caravana de migrantes sale de Tapachula rumbo al norte, dicen", published_at=now - timedelta(hours=29)),
            RawItem(url="https://mx.example/3", title="Caravana migrante llega a Huixtla", published_at=now - timedelta(hours=5)),
            RawItem(url="https://uk.example/4", title="Caravan holidays are booming in Devon", published_at=now - timedelta(hours=4)),
        ])
        conn.commit()
        process_new(conn)
        body = "TAPACHULA. Unos 1.500 migrantes salieron de Tapachula el jueves. Tras caminar 20 kilómetros, el grupo pernoctó en Álvaro Obregón."
        for n in (1, 2):
            conn.execute("""INSERT INTO item_texts (item_id, status, body, chars) SELECT id, 'ok', %s, %s FROM items WHERE url = %s""",
                         (body, len(body), f"https://mx.example/{n}"))
        conn.commit()
        llm = FakeLLM()
        assert run_movements(conn, llm=llm, max_seconds=60) == 4  # the holiday caravan is read but not asked about
        assert llm.calls == 3
        assert run_movements(conn, llm=llm, max_seconds=60) == 0  # nothing is read twice
        track = conn.execute("SELECT id, name FROM tracks WHERE kind = 'movement'").fetchone()
        rows = conn.execute("SELECT label, ST_Y(geom) lat, props FROM track_observations WHERE track_id = %s ORDER BY id", (track["id"],)).fetchall()
        labels = [r["label"] for r in rows]
        assert "Huixtla" in labels and "Tapachula" in labels and "Ciudad de México" not in labels
        assert "Huehuetán" not in labels and "Escuintla" not in labels  # wrong or made up quotes
        obregon = next(r for r in rows if r["label"].startswith("Álvaro"))
        assert 14.5 < obregon["lat"] < 15.5  # the Chiapas one, next to Tapachula, not Michoacan

    c = TestClient(app)
    s = c.get(f"/api/tracks/{track['id']}/state").json()
    assert s["summary"]["latest"]["place"] == "Huixtla" and s["summary"]["people"] == 1200 and s["summary"]["days"] == 2
    assert any(f["properties"]["category"] == "path" for f in s["features"]["features"])
    hux = next(f for f in s["features"]["features"] if f["properties"].get("label") == "Huixtla")
    assert c.post(f"/api/tracks/observations/{hux['properties']['id']}", json={"status": "rejected"}).json() == {"ok": True}
    s = c.get(f"/api/tracks/{track['id']}/state").json()
    assert s["summary"]["latest"]["place"] != "Huixtla"
