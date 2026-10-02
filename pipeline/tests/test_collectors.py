from datetime import datetime, timezone

from wassup.collectors.gdelt import _stamps_between, parse_gkg, parse_locations
from wassup.collectors.government import current_congress, parse_bills, parse_senate_votes
from wassup.collectors.rss import parse_feed


def _gkg_row(title="Russian strikes hit Kyiv power grid", themes="ARMEDCONFLICT;KILL;TAX_FNCACT_PRESIDENT",
             locs="4#Kyiv, Kyyiv, Misto, Ukraine#UP#UP12##50.4333#30.5167#-1044367#120;1#Russia#RS#RS##60#100#RS#40",
             url="https://kyivindependent.com/a", source="kyivindependent.com", translation=""):
    cols = [""] * 27
    cols[0], cols[1], cols[2], cols[3], cols[4] = "20261002170000-1", "20261002170000", "1", source, url
    cols[7] = themes
    cols[10] = locs
    cols[11] = "vladimir putin;volodymyr zelensky"
    cols[13] = "nato"
    cols[15] = "-5.2,1,6"
    cols[25] = translation
    cols[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(cols)


def test_parse_gkg_filters_by_theme_and_maps_fields():
    data = "\n".join([
        _gkg_row(),
        _gkg_row(title="Local bakery wins award", themes="TAX_FNCACT_BAKER", url="https://x.com/b"),
        _gkg_row(title="&#x41F;&#x443;&#x442;&#x438;&#x43D;", url="https://ria.ru/c", source="ria.ru", translation="srclc:rus;eng:GT"),
    ]).encode()
    items = parse_gkg(data, ["ARMEDCONFLICT", "KILL"], 1, feed="translingual")
    assert [i.url for i in items] == ["https://kyivindependent.com/a", "https://ria.ru/c"]
    a, b = items
    assert a.outlet_tier == "B" and not a.outlet_state
    assert b.outlet_tier == "S" and b.outlet_state and b.language == "ru" and b.title == "Путин"
    assert [p.name for p in a.places] == ["Russia", "Kyiv"]  # ordered by where the article mentions them
    assert a.places[1].key.startswith("gn:")  # snapped onto the gazetteer city
    assert ("person", "Vladimir Putin") in a.entities and ("org", "NATO") in a.entities
    assert a.meta["tone"] == -5.2


def test_parse_locations_orders_by_offset_and_drops_redundant_country():
    places = parse_locations("1#Ukraine#UP#UP##49#32#UP#300;4#Kyiv, Ukraine#UP#UP12##50.4333#30.5167#-1044367#10")
    assert [p.name for p in places] == ["Kyiv"]


def test_stamps_between():
    assert _stamps_between("20261002163000", "20261002171500", 8) == ["20261002164500", "20261002170000", "20261002171500"]
    assert len(_stamps_between(None, "20261002171500", 4)) == 4
    assert len(_stamps_between("20260901000000", "20261002171500", 4)) == 97  # capped at one day


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Iran and US resume talks in Oman - Example News</title><link>https://e.com/1</link>
<description>&lt;p&gt;Negotiators met in Muscat.&lt;/p&gt;</description><pubDate>Fri, 02 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Senate passes defense bill</title><link>https://e.com/2</link></item>
</channel></rss>"""


def test_parse_feed():
    items = parse_feed(RSS, {"key": "the_hill", "name": "The Hill", "tier": "B", "language": "en"})
    assert items[0].title == "Iran and US resume talks in Oman"
    assert items[0].summary == "Negotiators met in Muscat."
    assert items[0].published_at == datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
    assert {p.country for p in items[0].places} >= {"IR", "OM"}
    assert items[1].places[0].name == "Washington"  # default place for a US politics outlet


def test_congress_parsers():
    assert current_congress(datetime(2026, 10, 2, tzinfo=timezone.utc)) == (119, 2)
    bills = parse_bills({"bills": [{"type": "HR", "number": "1234", "congress": 119, "title": "Border Security Act",
                                    "latestAction": {"actionDate": "2026-09-30", "text": "Passed House"}}]})
    assert bills[0].url.startswith("https://www.congress.gov/bill/119th-congress/house-bill/1234")
    assert bills[0].outlet_tier == "A"
    xml = b"""<vote_summary><congress>119</congress><session>2</session><congress_year>2026</congress_year><votes>
      <vote><vote_number>00256</vote_number><vote_date>30-Sep</vote_date><issue>PN1129</issue><question>On the Nomination</question>
      <result>Confirmed</result><vote_tally><yeas>47</yeas><nays>41</nays></vote_tally><title>Confirmation: Jane Doe</title></vote>
    </votes></vote_summary>"""
    v = parse_senate_votes(xml, 119, 2)[0]
    assert v.title == "Senate vote 256: Confirmation: Jane Doe"
    assert "47 yeas, 41 nays" in v.summary
    assert v.url.endswith("vote_119_2_00256.htm")
