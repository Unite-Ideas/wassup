"""Shipping and trade: the freight network's numbers, live ships and planes, disruptions from the news."""
from datetime import date, datetime, timedelta, timezone

import pytest

from wassup.shipping import ais
from wassup.shipping.events import backed
from wassup.shipping.network import place_crossing, port_change
from wassup.shipping.planes import cargo_states


def test_border_crossings_placed_on_the_right_border():
    blaine = place_crossing("Blaine", "Pacific Highway", "Canadian Border")
    assert blaine and 48.9 < blaine[0] < 49.1
    assert place_crossing("Otay Mesa Port of Entry", "", "Mexican Border")[0] < 33
    assert place_crossing("El Paso", "Paso Del Norte (PDN)", "Mexican Border")[0] < 32
    assert place_crossing("Hidalgo/Pharr", "Pharr", "Mexican Border")[0] < 27
    # Laredo is in the town lists; the one by the Mexican border is chosen.
    laredo = place_crossing("Laredo", "World Trade Bridge", "Mexican Border")
    assert laredo and laredo[0] < 28.5
    assert place_crossing("Nowhere Special", "", "Mexican Border") is None


def test_port_change_only_counts_for_busy_ports():
    assert port_change({"calls_week": 8, "calls_normal": 15.0})["unusual"]
    assert port_change({"calls_week": 8, "calls_normal": 15.0})["change_pct"] == -47
    assert not port_change({"calls_week": 0, "calls_normal": 2.0})["unusual"]  # a quiet port: noise
    assert port_change({"calls_week": 3, "calls_normal": 0.0})["change_pct"] is None


def test_cargo_planes_by_callsign():
    states = [
        ["a1b2c3", "FDX1234 ", "United States", 1, 1760000000, -90.0, 35.0, 10000, False, 230, 90, 0, None, 10100, None, False, 0],
        ["d4e5f6", "DAL55   ", "United States", 1, 1760000000, -80.0, 33.0, 10000, False, 230, 90, 0, None, 10100, None, False, 0],
        ["0a0b0c", "CLX772", "Luxembourg", 1, 1760000000, None, None, 10000, False, 230, 90, 0, None, None, None, False, 0],
    ]
    rows = cargo_states(states, {"FDX": "FedEx Express", "CLX": "Cargolux"})
    assert [r[1] for r in rows] == ["FDX1234"]  # Delta is not cargo; the Cargolux plane has no position
    assert rows[0][2] == "FedEx Express" and round(rows[0][7]) == 447  # m/s to knots


def test_a_kind_needs_its_words_in_the_headlines():
    assert not backed("tariff", "Trump lets truckers use tax-free red diesel to cut fuel costs")
    assert backed("tariff", "US doubles duties on Chinese steel")
    assert not backed("attack_on_ship", "Saudi and Yemeni forces attack Houthis to retake the Red Sea coast")
    assert backed("attack_on_ship", "Russian drones attack civilian vessels off Bulgaria")
    assert backed("chokepoint", "anything")  # kinds without a word list pass


def _pos(m, lat=51.9, lon=4.1, sog=12.0):
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": m, "ShipName": "EVER TEST  ", "time_utc": "2026-10-09 18:11:38.1 +0000 UTC"},
            "Message": {"PositionReport": {"Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": 90.0, "TrueHeading": 511,
                                           "NavigationalStatus": 0, "UserID": m}}}


def _static(m, t):
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": m, "time_utc": "2026-10-09 18:11:40 +0000 UTC"},
            "Message": {"ShipStaticData": {"Type": t, "Name": "EVER TEST@@@", "Destination": "ROTTERDAM", "UserID": m,
                                           "Eta": {"Month": 10, "Day": 12, "Hour": 6, "Minute": 0}, "Dimension": {"A": 300, "B": 60},
                                           "ImoNumber": 9811000, "CallSign": "ABCD", "MaximumStaticDraught": 14.5}}}


def test_ais_keeps_cargo_ships_and_tankers_only():
    b = ais.Buffer([(70, 79), (80, 89)])
    for m in (1, 2, 3):
        b.add(_pos(m))
    b.add(_static(1, 70))
    b.add(_static(2, 30))   # a fishing boat
    b.add(_static(3, 84))
    b.add(_pos(2))          # dropped now its type is known
    b.add(_pos(4, lat=0, lon=0))  # no fix
    positions, statics, drop = b.take()
    assert set(positions) == {1, 3} and set(statics) == {1, 3} and drop == {2}
    assert statics[1]["name"] == "EVER TEST" and statics[1]["length_m"] == 360 and statics[1]["eta"] == "10-12 06:00"
    assert positions[1][4] is None  # heading 511 means not available
    assert b.take() == ({}, {}, set())


@pytest.mark.usefixtures("database")
def test_freight_network_ships_and_api():
    from fastapi.testclient import TestClient
    from psycopg.types.json import Jsonb

    from wassup import db
    from wassup.api import app
    from wassup.shipping.network import chokepoint_stats

    with db.connect() as conn:
        conn.execute("""INSERT INTO logistics_sites (key, kind, name, country, lat, lon, rank, info) VALUES
                        ('pw:chokepoint1', 'chokepoint', 'Suez Canal', 'EG', 30.59, 32.44, 6, '{}'),
                        ('pw:port1', 'port', 'Antwerp', 'BE', 51.26, 4.40, 4.5, '{"locode": "BE ANR"}'),
                        ('oa:EBBR', 'airport', 'Brussels Airport', 'BE', 50.90, 4.48, 2, '{"iata": "BRU"}')
                        ON CONFLICT (key) DO NOTHING""")
        # A normal year of 50 ships a day, then a week of 20: the canal is nearly shut.
        end = date.today() - timedelta(days=3)
        rows = [("pw:chokepoint1", end - timedelta(days=i), Jsonb({"total": 20 if i < 7 else 50, "tanker": 5})) for i in range(380)]
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO chokepoint_days (key, day, counts) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", rows)
        chokepoint_stats(conn)
        s = conn.execute("SELECT stats FROM logistics_sites WHERE key = 'pw:chokepoint1'").fetchone()["stats"]
        assert s["transits_week"] == 140 and s["transits_normal"] == 350 and s["change_pct"] == -60 and s["unusual"]
        # Ships: one cargo ship seen now, one tanker across the date line, one fishing boat.
        positions = {1: (51.9, 4.1, 12.0, 90.0, None, 0, datetime.now(timezone.utc), "EVER TEST"),
                     2: (40.0, 179.5, 0.0, 0.0, None, 1, datetime.now(timezone.utc), "PACIFIC"),
                     3: (51.0, 3.0, 5.0, 10.0, None, 7, datetime.now(timezone.utc), "TRAWLER")}
        now = datetime.now(timezone.utc)
        statics = {1: {"name": "EVER TEST", "imo": 1, "callsign": "A", "ship_type": 70, "length_m": 300, "destination": "ROTTERDAM",
                       "eta": None, "draught": 14.0, "at": now},
                   2: {"name": "PACIFIC", "imo": 2, "callsign": "B", "ship_type": 80, "length_m": 250, "destination": "BUSAN",
                       "eta": None, "draught": 12.0, "at": now},
                   3: {"name": "TRAWLER", "imo": None, "callsign": "C", "ship_type": 30, "length_m": 20, "destination": None,
                       "eta": None, "draught": None, "at": now}}
        ais.flush(conn, positions, statics, set())
        ais.upkeep(conn)
        assert conn.execute("SELECT count(*) AS n FROM vessel_track").fetchone()["n"] == 2  # cargo and tanker only
        conn.execute("""INSERT INTO rail_lines (geom) VALUES (ST_Transform(ST_GeomFromText('LINESTRING(4 50, 5 51)', 4326), 3857))""")
        conn.commit()

    client = TestClient(app)
    sites = client.get("/api/shipping/sites?kinds=chokepoint,port").json()["features"]
    assert {f["properties"]["name"] for f in sites} >= {"Suez Canal", "Antwerp"}
    suez = next(f for f in sites if f["properties"]["name"] == "Suez Canal")
    card = client.get(f"/api/shipping/sites/{suez['properties']['id']}").json()
    assert len(card["series"]) >= 365 and card["stats"]["unusual"]
    near = client.get("/api/shipping/vessels?bbox=0,45,10,55").json()["features"]
    assert [f["properties"]["mmsi"] for f in near] == [1]  # the trawler is not shown
    across = client.get("/api/shipping/vessels?bbox=170,30,-170,50").json()["features"]
    assert [f["properties"]["kind"] for f in across] == ["tanker"]
    v = client.get("/api/shipping/vessels/1").json()
    assert v["type_label"] == "cargo ship" and v["destination"] == "ROTTERDAM" and len(v["track"]) >= 1
    tile = client.get("/api/shipping/tiles/rail/5/16/10.mvt")
    assert tile.status_code == 200 and len(tile.content) > 0
    st = client.get("/api/shipping/status").json()
    assert st["ais"]["ships"] == 2 and st["rail"]


class Reader:
    """Stands in for the local model: the dock strike is a disruption at Antwerp; the earnings report is not."""

    def __call__(self, prompt, schema):
        out = []
        for ln in prompt.splitlines():
            if ln.startswith("["):
                n = int(ln[1:ln.index("]")])
                if "strike" in ln.lower():
                    out.append({"n": n, "disruption": True, "kind": "labour_strike", "place": "Port of Antwerp", "country": "BE",
                                "severity": 2, "status": "ongoing", "summary": "Dockworkers walked out at Antwerp."})
                else:
                    out.append({"n": n, "disruption": False, "kind": "other", "place": "", "country": "", "severity": 1,
                                "status": "ended", "summary": ""})
        return {"stories": out}


@pytest.mark.usefixtures("database")
def test_shipping_desk_and_disruptions_from_the_news():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.shipping.events import run_events
    from wassup.triage import run_triage

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute("""INSERT INTO logistics_sites (key, kind, name, country, lat, lon, rank) VALUES
                        ('pw:port1', 'port', 'Antwerp', 'BE', 51.26, 4.40, 4.5) ON CONFLICT (key) DO NOTHING""")
        sid = ensure_source(conn, "shipping-test", "Shipping test", "rss")
        store_items(conn, sid, [
            RawItem(url="https://s.example/1", title="Dockworkers strike shuts the port of Antwerp as container ships queue",
                    summary="Container terminal closed by the port strike; freight backlog grows.", published_at=now - timedelta(hours=2)),
            RawItem(url="https://s.example/2", title="Dockworkers strike shuts the port of Antwerp, container ships queue offshore",
                    summary="Longshoremen at the container terminal walked out.", published_at=now - timedelta(hours=1)),
            RawItem(url="https://s.example/3", title="Container shipping line posts record quarterly profit on high freight rates",
                    summary="The ocean carriers' container rates kept profits high.", published_at=now - timedelta(hours=1)),
        ])
        conn.commit()
        process_new(conn)
        run_triage(conn)
        desks = {r["title"][:20]: r["desk"] for r in conn.execute("SELECT title, desk FROM stories WHERE routed")}
        assert desks.get("Dockworkers strike s") == "shipping"
        assert run_events(conn, llm=Reader()) >= 1
        e = conn.execute("SELECT * FROM shipping_events WHERE source = 'news' AND kind <> 'none'").fetchall()
        assert len(e) == 1 and e[0]["kind"] == "labour_strike" and e[0]["place"] == "Antwerp" and abs(e[0]["lat"] - 51.26) < 0.01
        assert run_events(conn, llm=Reader()) == 0  # read once until the story doubles
        # A strait the headlines name wins over the country beside it.
        from wassup.shipping.events import locate
        assert locate(conn, "Iran", "IR", {}, "Tanker struck by projectile in Strait of Hormuz")[2] == "Strait of Hormuz"
        assert locate(conn, "Bulgaria", "BG", {}, "Russia strikes civilian vessels in Black Sea off Bulgaria")[2] == "Black Sea"
        assert locate(conn, "Port of Antwerp", "BE", {}, "Dock strike in Antwerp")[2] == "Antwerp"
    feats = TestClient(app).get("/api/shipping/events").json()["features"]
    assert [f["properties"]["kind_label"] for f in feats if f["properties"]["source"] == "news"] == ["Strike or labour action"]
