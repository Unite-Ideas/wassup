"""Reading the full text of articles."""
from datetime import datetime, timezone

import httpx
import pytest

ARTICLE = """<html><head><title>Bridge hit</title><meta property="og:image" content="/img/lead.jpg"></head><body>
<nav>Home | World | Sport</nav>
<article><h1>Russian drone strikes Kyiv bridge</h1>
<p>A Russian drone struck the Northern Bridge in Kyiv early on Monday, officials said, snarling traffic across the city.</p>
<p>Engineers were inspecting the damage, and two lanes were closed in each direction while police diverted traffic to other crossings.</p>
<p>The strike came hours after the German chancellor arrived in the capital by train for talks on air defence and drone production.</p>
<figure><img src="/img/bridge.jpg"><figcaption>Smoke rises over the Northern Bridge in Kyiv on Monday.</figcaption></figure>
<p>Ukraine's air force said it shot down most of the drones launched overnight, but several reached the city.</p>
</article><footer>Subscribe to our newsletter</footer></body></html>"""


def test_extract_text_lead_image_and_captions():
    from wassup.reader import extract

    x = extract("https://news.example/world/bridge", ARTICLE)
    assert "Northern Bridge in Kyiv early on Monday" in x["text"] and "Subscribe" not in x["text"]
    assert x["lead_image"] == "https://news.example/img/lead.jpg"
    assert x["images"] == [{"src": "https://news.example/img/bridge.jpg", "caption": "Smoke rises over the Northern Bridge in Kyiv on Monday."}]


@pytest.mark.usefixtures("database")
def test_reader_stores_text_and_records_blocked_pages(monkeypatch):
    from wassup import db, reader
    from wassup.collectors.base import RawItem, ensure_source, store_items

    def handler(req):
        if "paywalled" in req.url.host:
            return httpx.Response(403, text="no")
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        sid = ensure_source(conn, "reader-test", "Reader test", "rss")
        store_items(conn, sid, [RawItem(url="https://open.example/a", title="Russian drone strikes Kyiv bridge", published_at=now),
                                RawItem(url="https://paywalled.example/b", title="Russian drone strikes Kyiv bridge again", published_at=now)])
        conn.commit()
        from wassup.cluster import process_new
        process_new(conn)
        conn.execute("UPDATE stories SET routed = true WHERE id IN (SELECT story_id FROM items WHERE url LIKE '%%.example/%%')")
        conn.commit()
        r = reader.Reader(client=httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            assert r.run(conn, max_seconds=20) >= 2
        finally:
            r.pool.shutdown()
        rows = {x["url"]: x for x in conn.execute(
            "SELECT i.url, t.status, t.http_status, t.chars FROM item_texts t JOIN items i ON i.id = t.item_id WHERE i.url LIKE '%%.example/%%'")}
        assert rows["https://open.example/a"]["status"] == "ok" and rows["https://open.example/a"]["chars"] > 300
        assert rows["https://paywalled.example/b"]["status"] == "blocked"
        assert r.run(conn, max_seconds=5) == 0  # nothing is read twice
