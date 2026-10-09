"""Who holds what: reading Wikipedia control maps and official zones."""
import io
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from wassup.tracks.control import classify, colors_in, kind_of, palette, parse_legend, parse_marks, wanted_revisions

MODULE = """return {
	marks = {
        { lat = "15.380", long = "44.209", mark = "Dot green 0d0.svg", marksize = "32", label = "[[Sanaa#Contemporary era|Sanaa]]", link = "Sanaa" },
		{ lat = "12.785", long = "45.018", mark = "Location dot red.svg", marksize = "30", label = "[[Aden]]", link = "Aden" },
		-- { lat = "13.0", long = "44.0", mark = "Location dot red.svg", label = "[[Commented out]]" },
		{ lat = "13.578", long = "44.017", mark = "80x80-red-lime-anim.gif", marksize = "20", label = "[[Taiz]]" },
		{ lat = "15.230", long = "44.059", mark = "Abm-lime-icon.png", marksize = "8", label = "[[Attan Missile Base]]" },
		{ lat = "14.5", long = "49.1", mark = "Map-dot-grey-68a.svg", marksize = "6", label = "[[Al Mukalla]]" },
		{ lat = "16.0", long = "48.0", mark = "Gota01.svg", label = "[[Oil field]]" },
		{ lat = "16.5", long = "44.5", mark = "Location dot blue.svg", marksize = "6", label = "[[Tribal town]]" },
	},
	containerArgs = {
		'Yemen',
		caption = [=[Hold cursor over location to display name; click [[File:Pointing hand cursor vector.svg|25px]].<br/>
*[[File:Location dot red.svg|11px]] Internationally recognized [[Cabinet of Yemen|government]] and coalition
*[[File:Dot green 0d0.svg|11px]] [[Houthis|Ansarullah (Houthis)]] and allies
*[[File:Map-dot-grey-68a.svg|11px]] Sunni jihadists
Contested
*[[File:80x80-red-lime-anim.gif|11px]] Pro-Government &nbsp;- Houthis
]=],
	}
}"""

DOC_TABLE = """{| border="1"
!Military force
!City
|-
|[[Tribal forces|Tribal forces]]
|[[File:Location dot blue.svg|11px]]
|[[File:Abm-blue-icon.png|13px]]
|-
|}"""


def test_reading_a_control_map():
    assert colors_in("80x80-red-lime-anim.gif") == ["red", "lime"] and colors_in("Location dot dark red.svg") == ["dark red"]
    assert kind_of("Abm-lime-icon.png") == "base" and kind_of("Gota01.svg") is None and kind_of("Map-ctl2-red+lime.svg") == "mixed"
    marks = parse_marks(MODULE)
    assert len(marks) == 7 and not any(m["label"] == "Commented out" for m in marks)
    assert marks[0]["label"] == "Sanaa" and marks[0]["lat"] == 15.38
    legend = parse_legend(MODULE)
    for f, side in parse_legend(DOC_TABLE).items():  # a legend on the documentation page
        legend.setdefault(f, side)
    places = {p["label"]: p for p in classify(marks, legend)}
    assert places["Sanaa"]["side"] == "Ansarullah (Houthis) and allies"
    assert places["Aden"]["side"].startswith("Internationally recognized government")
    assert places["Attan Missile Base"]["side"] == "Ansarullah (Houthis) and allies"  # lime icon, green legend
    assert places["Taiz"]["kind"] == "contested" and places["Taiz"]["side"] is None and len(places["Taiz"]["sides"]) == 2
    assert places["Tribal town"]["side"] == "Tribal forces"
    assert "Oil field" not in places  # infrastructure is not control
    pal = palette(legend)
    assert pal["Ansarullah (Houthis) and allies"]["color"] == "green" and pal["Sunni jihadists"]["color"] == "grey"


def test_history_one_map_a_day_then_a_week():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    revs = [{"revid": i, "at": now - timedelta(hours=7 * i)} for i in range(1, 2000)]  # four edits a day for a year
    want = wanted_revisions(revs, now, daily_days=30, weekly_days=200)
    days = {r["at"].date() for r in want if (now - r["at"]).days <= 30}
    assert len(days) == len([r for r in want if (now - r["at"]).days <= 30])  # one a day
    older = [r for r in want if (now - r["at"]).days > 30]
    assert 20 <= len(older) <= 26 and all((now - r["at"]).days <= 200 for r in want)
    assert want[0]["revid"] == 1  # the newest edit of the newest day


class FakeWiki:
    def __init__(self, revisions):
        self.revs = revisions  # revid -> (time, text)

    def latest(self, titles):
        rid = max(self.revs)
        return {t: {"revid": rid, "at": self.revs[rid][0]} for t in titles}

    def history(self, title, since):
        return [{"revid": r, "at": t} for r, (t, _) in sorted(self.revs.items(), reverse=True)]

    def raw(self, title, revid=None):
        return self.revs[revid][1]

    def raw_as_of(self, title, at):
        return DOC_TABLE


@pytest.mark.usefixtures("database")
def test_control_maps_from_wikipedia_to_the_map(monkeypatch):
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.tracks import control

    monkeypatch.setattr(control, "load_yaml", lambda name: {
        "wikipedia": {"daily_days": 120, "weekly_days": 730, "maps_per_run": 10},
        "conflicts": [{"key": "yemen-test", "name": "Yemen", "module": "Yemeni Civil War detailed map", "desk": "iran_mideast"}]})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    older = MODULE.replace('mark = "Location dot red.svg", marksize = "30", label = "[[Aden]]"',
                           'mark = "Dot green 0d0.svg", marksize = "30", label = "[[Aden]]"')
    wiki = FakeWiki({101: (now - timedelta(days=10), older), 102: (now - timedelta(days=1), MODULE)})
    with db.connect() as conn:
        assert control.run_control(conn, wiki=wiki) == 2  # the latest map, then the history
        assert control.run_control(conn, wiki=wiki) == 0  # nothing twice
        track = conn.execute("SELECT id FROM tracks WHERE key = 'control-yemen-test'").fetchone()["id"]
        held = conn.execute("SELECT label, ST_Area(geom::geography) / 1e6 AS km2 FROM track_observations WHERE track_id = %s "
                            "AND category = 'held' AND snapshot_id = (SELECT id FROM track_snapshots WHERE track_id = %s ORDER BY observed_at DESC LIMIT 1)",
                            (track, track)).fetchall()
        assert {h["label"] for h in held} >= {"Ansarullah (Houthis) and allies", "Internationally recognized government and coalition"}
        assert all(0 < h["km2"] < 2000 for h in held)  # cut to circles, not the whole country

    client = TestClient(app)
    st = client.get(f"/api/tracks/{track}/state", params={"compare_days": 7}).json()
    assert st["kind"] == "control" and st["snapshot"]["stats"]["revision"] == 102
    assert st["snapshot"]["stats"]["source_url"].endswith("oldid=102")
    places = {f["properties"]["label"]: f["properties"] for f in st["features"]["features"] if f["properties"]["category"] == "place"}
    assert places["Aden"]["changed_from"] == "Ansarullah (Houthis) and allies" and st["summary"]["changed"] == 1
    assert places["Taiz"]["hex2"] and "hexes" not in places["Taiz"]  # plain values for the map
    info = client.get(f"/api/tracks/{track}/places/{places['Aden']['id']}").json()
    assert info["side"].startswith("Internationally recognized") and info["before"] == "Ansarullah (Houthis) and allies"
    assert info["since"].startswith((now - timedelta(days=1)).date().isoformat())
    sanaa = client.get(f"/api/tracks/{track}/places/{places['Sanaa']['id']}").json()
    assert sanaa["at_least"] and sanaa["before"] is None  # held on every map Wassup has


def _zipped_shapefile() -> bytes:
    import shapefile

    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    w.field("CLASS", "C")
    w.poly([[[35.2, 31.9], [35.3, 31.9], [35.3, 32.0], [35.2, 32.0], [35.2, 31.9]]])
    w.record("A")
    w.poly([[[35.3, 31.9], [35.4, 31.9], [35.4, 32.0], [35.3, 32.0], [35.3, 31.9]]])
    w.record("C")
    w.close()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Oslo.shp", shp.getvalue())
        z.writestr("Oslo.shx", shx.getvalue())
        z.writestr("Oslo.dbf", dbf.getvalue())
        z.writestr("Oslo.prj", 'GEOGCS["GCS_WGS_1984"]')
    return out.getvalue()


@pytest.mark.usefixtures("database")
def test_official_zones():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.tracks.zones import load_zone

    data = _zipped_shapefile()

    def handler(req):
        if "package_show" in str(req.url):
            return httpx.Response(200, json={"result": {"title": "Oslo", "dataset_source": "PA MOP", "last_modified": "2019-07-22T12:24:39.2",
                                                        "resources": [{"format": "SHP", "url": "https://hdx.example/oslo.zip"}]}})
        return httpx.Response(200, content=data)

    z = {"key": "wb-test", "name": "West Bank zones", "hdx": "oslo-test", "field": "CLASS", "note": "test"}
    with db.connect() as conn:
        assert load_zone(conn, z, httpx.Client(transport=httpx.MockTransport(handler))) == 1
        assert load_zone(conn, z, httpx.Client(transport=httpx.MockTransport(handler))) == 0  # refreshed monthly
        track = conn.execute("SELECT id FROM tracks WHERE key = 'zones-wb-test'").fetchone()["id"]
    st = TestClient(app).get(f"/api/tracks/{track}/state").json()
    zones = st["snapshot"]["stats"]["zones"]
    assert set(zones) == {"A", "C"} and zones["C"]["name"].startswith("Area C") and 90 < zones["A"]["km2"] < 110
    assert {f["properties"]["category"] for f in st["features"]["features"]} == {"zone"}
