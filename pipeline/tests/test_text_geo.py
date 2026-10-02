from wassup.geo import gazetteer
from wassup.outlets import tier_for
from wassup.text import PhraseMatcher, clean_title


def test_phrase_matcher_whole_words_and_scripts():
    m = PhraseMatcher(["ice raids", "iran", "путин", "white house"])
    assert m.find("ICE raids in Chicago; Iran responds") == ["ice raids", "iran"]
    assert m.find("Iranian officials") == []  # whole words only
    assert m.find("Путин встретился") == ["путин"]
    assert m.find("At the White House today") == ["white house"]


def test_clean_title_strips_site_names_and_entities():
    assert clean_title("G7 agrees to release 100 million barrels of oil | Honolulu Star-Advertiser") == "G7 agrees to release 100 million barrels of oil"
    assert clean_title("Trump - Putin meeting set for next week in Budapest - Reuters") == "Trump - Putin meeting set for next week in Budapest"
    assert clean_title("&#x41A;&#x43B;&#x438;&#x447;&#x43A;&#x43E;: test") == "Кличко: test"
    # A real dash in the headline is kept
    assert clean_title("Russia-Ukraine war: what we know on day 1,316 - live updates") == "Russia-Ukraine war: what we know on day 1,316 - live updates"


def test_gazetteer_prefers_cities_and_skips_ambiguous_names():
    g = gazetteer()
    names = [p.name for p in g.find("Russian drones strike Kyiv as Zelensky meets Trump at the White House")]
    assert names[:2] == ["Kyiv", "Washington"]
    assert "Russia" in names
    assert g.find("Georgia senator votes against bill") == []
    assert [p.country for p in g.find("Turkey hosts talks in Istanbul")] == ["TR"]


def test_gazetteer_fips_and_snap():
    g = gazetteer()
    assert g.fips_to_iso["UP"] == "UA"
    assert g.snap_city(50.45, 30.52, "UA").name == "Kyiv"
    assert g.snap_city(50.45, 30.52, "RU") is None


def test_outlet_tiers():
    assert tier_for("https://www.rt.com/news/123") == ("S", True)
    assert tier_for("edition.cnn.com") == ("B", False)
    assert tier_for("https://www.defense.gov/News/") == ("A", False)
    assert tier_for("spa.gov.sa") == ("S", True)
    assert tier_for("somerandomblog.net") == ("U", False)
