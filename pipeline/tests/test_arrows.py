"""Attack arrows explained: where they point, how long a push has lasted, ground around it."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.tracks.arrows import _compass, where


def test_where_an_arrow_points():
    # Just west of Pokrovsk, heading east: Pokrovsk lies ahead.
    w = where(48.28, 37.05, 90)
    assert w["towards"]["name"] == "Pokrovsk" and w["towards"]["admin1"] == "Donetsk"
    assert w["near"]["km"] < 15
    # The same spot heading west points away from it.
    assert where(48.28, 37.05, 270)["towards"]["name"] != "Pokrovsk"
    assert _compass(0) == "north" and _compass(100) == "east" and _compass(337.5) == "north" and _compass(None) is None


@pytest.mark.usefixtures("database")
def test_attack_details_from_the_maps():
    from fastapi.testclient import TestClient
    from psycopg.types.json import Jsonb

    from wassup import db
    from wassup.api import app

    t0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    with db.connect() as conn:
        track = conn.execute("""INSERT INTO tracks (key, name, kind) VALUES ('test-front', 'Test front', 'front') RETURNING id""").fetchone()["id"]
        arrow = None
        for day in range(0, 31):
            when = t0 + timedelta(days=day)
            snap = conn.execute("INSERT INTO track_snapshots (track_id, observed_at, source_ref) VALUES (%s, %s, %s) RETURNING id",
                                (track, when, f"s{day}")).fetchone()["id"]
            # The occupied area grows east to west by about 1 km a day near the arrow: a week is
            # 7 km across a circle 30 km wide, about 190 km².
            west = 37.20 - 0.013 * day
            conn.execute("""INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom)
                            VALUES (%s, %s, %s, 'occupied', ST_MakeEnvelope(%s, 48.1, 37.6, 48.4, 4326))""", (track, snap, when, west))
            if day >= 10:  # the push is marked from day 10
                arrow = conn.execute("""INSERT INTO track_observations (track_id, snapshot_id, observed_at, category, geom, props)
                                        VALUES (%s, %s, %s, 'attack', ST_SetSRID(ST_MakePoint(%s, 48.28), 4326), %s) RETURNING id""",
                                     (track, snap, when, west, Jsonb({"bearing": 270.0}))).fetchone()["id"]
        conn.commit()
    client = TestClient(app)
    feats = client.get(f"/api/tracks/{track}/state", params={"at": (t0 + timedelta(days=30, hours=1)).isoformat()}).json()["features"]["features"]
    clickable = [f["properties"] for f in feats if f["properties"]["category"] == "attack"]
    assert clickable and clickable[0]["id"] == arrow and clickable[0]["track_id"] == track
    info = client.get(f"/api/tracks/{track}/attacks/{arrow}").json()
    assert info["heading"] == "west" and info["towards"]
    p = info["persistence"]
    assert p["since"] == (t0 + timedelta(days=10)).date().isoformat() and p["run_days"] == 21 and not p["at_least"]
    assert p["marked_last_year"] == 21 and p["maps_last_year"] == 31
    assert 100 < info["ground"]["week"]["taken_km2"] < 250 and info["ground"]["week"]["retaken_km2"] == 0
    assert info["ground"]["month"]["taken_km2"] > info["ground"]["week"]["taken_km2"]
    assert client.get(f"/api/tracks/{track}/attacks/999999").status_code == 404
