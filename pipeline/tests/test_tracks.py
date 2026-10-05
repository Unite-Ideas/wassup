"""Map tiles, the DeepStateMap front importer, and the tracks API."""
import gzip
from datetime import datetime, timedelta, timezone

import pytest

from wassup.tracks.deepstate import classify, daily_snapshots


def _poly(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _feat(name, geom, **props):
    return {"type": "Feature", "geometry": geom, "properties": {"name": name, **props}}


def test_classify_current_and_2022_formats():
    assert classify(_feat("Окуповано /// Occupied /// geoJSON.status.occupied", _poly(0, 0, 1, 1)))[0] == "occupied"
    assert classify(_feat("ОРДЛО /// CADR and CALR /// geoJSON.territories.ordlo", _poly(0, 0, 1, 1)))[0] == "occupied"
    assert classify(_feat("Тимчасово окупована східна Пруссія /// geoJSON.territories.prussia", _poly(0, 0, 1, 1)))[0] is None
    assert classify(_feat("Статус невідомий /// Unknown status /// geoJSON.status.unknown", _poly(0, 0, 1, 1)))[0] == "contested"
    cat, props = classify(_feat("Напрямок удару /// Direction of attack /// geoJSON.status.attack_direction",
                                {"type": "Point", "coordinates": [37, 48, 0]}, description="{icon=arrow_12}"))
    assert cat == "attack" and props["bearing"] == 270
    # 2022: no tags, Ukrainian names and fill colours only.
    assert classify(_feat("Окуповано. 01.04", _poly(0, 0, 1, 1), fill="#a52714"))[0] == "occupied"
    assert classify(_feat("Під питанням", _poly(0, 0, 1, 1)))[0] == "contested"
    assert classify(_feat("Звільнено", _poly(0, 0, 1, 1)))[0] == "liberated"
    assert classify(_feat("Придністров'я", _poly(0, 0, 1, 1), fill="#880e4f"))[0] is None
    assert classify(_feat("Окуповані у Естонії території", _poly(0, 0, 1, 1)))[0] is None
    assert classify(_feat("Something unnamed", _poly(0, 0, 1, 1), fill="#BCAAA4"))[0] == "contested"


def test_daily_snapshots_keeps_last_of_each_kyiv_day():
    h = [{"id": 1, "createdAt": "2026-10-03T08:00:00.000Z"}, {"id": 2, "createdAt": "2026-10-03T19:00:00.000Z"},
         {"id": 3, "createdAt": "2026-10-03T22:30:00.000Z"}]  # 01:30 on Oct 4 in Kyiv
    assert [s["id"] for s in daily_snapshots(h)] == ["3", "2"]


def test_tiles_from_pmtiles_files(tmp_path, monkeypatch):
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    def write(name, tiles, bbox):
        with open(tmp_path / f"{name}.pmtiles", "wb") as f:
            w = Writer(f)
            for (z, x, y), data in sorted(tiles.items(), key=lambda kv: zxy_to_tileid(*kv[0])):
                w.write_tile(zxy_to_tileid(z, x, y), gzip.compress(data))
            w.finalize({"tile_type": TileType.MVT, "tile_compression": Compression.GZIP,
                        "min_lon_e7": int(bbox[0] * 1e7), "min_lat_e7": int(bbox[1] * 1e7),
                        "max_lon_e7": int(bbox[2] * 1e7), "max_lat_e7": int(bbox[3] * 1e7)}, {})

    write("world", {(0, 0, 0): b"world0", (1, 1, 0): b"world1"}, (-180, -85, 180, 85))
    write("kyiv", {(8, 149, 86): b"kyiv8"}, (30.2, 50.3, 30.8, 50.6))
    from wassup import maps

    s = maps.MapStore(tmp_path)
    assert s.world and [r.name for r in s.regions] == ["kyiv"]
    assert gzip.decompress(s.tile("world", 1, 1, 0)[0]) == b"world1"
    assert s.tile("world", 1, 0, 0)[0] is None
    data, gz = s.tile("detail", 8, 149, 86)
    assert gz and gzip.decompress(data) == b"kyiv8"
    assert s.tile("detail", 8, 10, 10)[0] is None  # not covered by any region
    assert s.tile("detail", 3, 4, 2)[0] is None    # below the detail zooms


@pytest.mark.usefixtures("database")
def test_front_snapshots_and_state_api():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.tracks.deepstate import ensure_track, store_snapshot

    t0 = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
    def snap(east):
        return {"type": "FeatureCollection", "features": [
            _feat("Окуповано /// Occupied /// geoJSON.status.occupied", _poly(37.0, 47.0, east, 48.0)),
            _feat("ОРДЛО /// geoJSON.territories.ordlo", _poly(38.0, 47.0, 39.0, 48.0)),
            _feat("Статус невідомий /// geoJSON.status.unknown", _poly(36.9, 47.0, 37.0, 48.0)),
            _feat("Напрямок удару /// geoJSON.status.attack_direction", {"type": "Point", "coordinates": [36.95, 47.5, 0]},
                  description="{icon=arrow_12}"),
        ]}
    with db.connect() as conn:
        track = ensure_track(conn)
        store_snapshot(conn, track, "a", t0, snap(38.0))
        store_snapshot(conn, track, "b", t0 + timedelta(days=1), snap(38.0))
        store_snapshot(conn, track, "c", t0 + timedelta(days=2), snap(37.9))  # the occupier lost a strip
        conn.commit()
        stats = conn.execute("SELECT stats FROM track_snapshots WHERE source_ref = 'a'").fetchone()["stats"]
        assert 16000 < stats["occupied_km2"] < 17500 and stats["attacks"] == 1  # two 1 x 1 degree squares, joined

    c = TestClient(app)
    tr = next(t for t in c.get("/api/tracks").json() if t["key"] == "ukraine-front")
    s = c.get(f"/api/tracks/{tr['id']}/state", params={"at": (t0 + timedelta(days=2, hours=1)).isoformat()}).json()
    cats = [f["properties"]["category"] for f in s["features"]["features"]]
    assert s["snapshot"]["stats"]["occupied_km2"] < stats["occupied_km2"]
    assert "lost" in cats and "gained" not in cats and "attack" in cats and "contested" in cats
    lost = next(f for f in s["features"]["features"] if f["properties"]["category"] == "lost")
    assert 500 < lost["properties"]["km2"] < 1000  # a 0.1 x 1 degree strip
    # No change the day before.
    s = c.get(f"/api/tracks/{tr['id']}/state", params={"at": (t0 + timedelta(days=1, hours=1)).isoformat()}).json()
    assert not [f for f in s["features"]["features"] if f["properties"]["category"] in ("gained", "lost")]
    assert len(c.get(f"/api/tracks/{tr['id']}/series").json()) == 3
