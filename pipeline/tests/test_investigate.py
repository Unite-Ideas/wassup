"""Investigations: reading links of every kind, following citations, and the summary."""
from datetime import datetime, timezone

import httpx
import pytest

from wassup.investigate import core
from wassup.investigate.fetch import canonical, date_in_url, fetch, kind_of, parse_article

ARTICLE = """<html><head><title>Board backs pastor</title>
<meta property="og:title" content="Board backs pastor accused of misconduct">
<meta property="og:site_name" content="Example News">
<meta property="article:published_time" content="2010-03-02T10:00:00Z">
</head><body><nav><a href="/news/">News</a><a href="/tag/church/">Church</a></nav>
<article><h1>Board backs pastor accused of misconduct</h1>
<p>The church board said on Sunday it believes the pastor, after a former member published documents he says show
misconduct on ministry trips. The board did not say whether it had reviewed the documents itself.</p>
<p>The allegations were first reported by <a href="https://report.example/exclusive-pastor-documents">Example Report</a>,
which said it reviewed the messages and confirmed travel dates against the pastor's calendar. A forensic firm's
<a href="https://files.example/letter.pdf">letter</a> said the files appeared authentic.</p>
<p>The pastor denies the allegations and has hired a lawyer. <a href="https://example.com/share?u=1">Share</a>
<a href="https://x.com/ExampleNews">Follow us</a> <a href="https://x.com/someone/status/12345">a post</a></p>
<p>More context about the organisation and the timeline of the complaint, which went to the district in May and was
closed in July, follows in later reporting.</p>
</article></body></html>"""


def test_kinds_and_canonical_addresses():
    assert kind_of("https://youtu.be/abc123") == "youtube"
    assert canonical("https://youtu.be/abc123?t=40") == "https://www.youtube.com/watch?v=abc123"
    assert canonical("https://twitter.com/user/status/99?s=20") == "https://x.com/user/status/99"
    assert canonical("https://t.me/s/channel_x/77") == "https://t.me/channel_x/77"
    assert canonical("https://site.example/a?utm_source=fb&id=3#top") == "https://site.example/a?id=3"
    assert kind_of("https://www.facebook.com/groups/1/posts/2/") == "facebook"


def test_dates_in_addresses():
    assert date_in_url("https://www.projectrescue.example/9-28-26-statement").date().isoformat() == "2026-09-28"
    assert date_in_url("https://protestia.example/2026/09/30/story/").date().isoformat() == "2026-09-30"
    assert date_in_url("https://example.com/about") is None


def test_article_text_date_and_the_links_it_relies_on():
    d = parse_article("https://news.example.com/2026/09/30/board-backs-pastor/", ARTICLE)
    assert d["title"].startswith("Board backs pastor") and "forensic firm" in d["text"]
    assert d["published_at"].date().isoformat() == "2026-09-30"  # the page's own date is a template's; the address wins
    urls = [l["url"] for l in d["links"]]
    assert "https://report.example/exclusive-pastor-documents" in urls and "https://files.example/letter.pdf" in urls
    assert "https://x.com/someone/status/12345" in urls
    assert not any("share" in u or u.endswith("/ExampleNews") or "/tag/" in u for u in urls)  # buttons, profiles, sections


def test_facebook_preview_is_read_as_partial():
    page = ('<meta property="og:title" content="Julie Roys"><meta property="og:description" content="A pastor raising '
            'millions should not be seeking sexual services on those same trips. Yet documents appear to show...">')
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    d = fetch("https://www.facebook.com/someone/posts/123/", client)
    assert d["kind"] == "facebook" and d["partial"] and "documents appear to show" in d["text"]
    closed = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<meta property="og:title" content="Log in to Facebook">')))
    with pytest.raises(Exception, match="logged in"):
        fetch("https://www.facebook.com/groups/1/posts/2/", closed)


def test_pasted_text_is_cleaned():
    pasted = "Julie Roys\n \nr͏s͏e͏\nM͏\n0͏g͏u͏\n ·\nA pastor raising millions to rescue women.\n\n\n\nMore."
    out = core.clean_pasted(pasted)
    assert out.startswith("Julie Roys") and "͏" not in out and "\nM\n" not in out and "\n\n\n" not in out


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        if "careful investigative editor" in prompt:
            return {"headline": "Documents described; pastor denies", "answers": [
                        {"question": "What evidence exists?", "answer": "Messages a former member says he found.", "confidence": "medium", "sources": [1]}],
                    "evidence": [{"what": "Messages", "held_by": "former member", "status": "seen by a reporter", "detail": "", "sources": [1]}],
                    "people": [], "timeline": [], "origin": "First reported by Example Report.", "disagreements": [], "open_questions": []}
        if "Write web search queries" in prompt:
            return {"queries": [{"query": "pastor ministry trips allegations", "language": "en"}]}
        relevant = "unrelated weather" not in prompt
        if "contradicts itself" in prompt:  # says it is reporting on the story, and also not relevant
            return {**self(prompt.replace("contradicts itself", ""), schema), "relevant": False, "account": "original_reporting"}
        return {"relevant": relevant, "account": "secondhand" if relevant else "unrelated",
                "summary": "Reports the board's support for the pastor.", "relies_on": ["Example Report"],
                "evidence": [], "people": [], "claims": [], "responses": [],
                "cited_links": [1, 2, 99], "dates": []}


@pytest.mark.usefixtures("database")
def test_investigation_from_links_to_summary(monkeypatch):
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app

    pages = {
        "https://news.example.com/2026/09/30/board-backs-pastor/": ("article", ARTICLE),
        "https://weather.example/today": ("article", None),
    }

    def fake_fetch(url, client=None):
        if url in pages and pages[url][1]:
            return parse_article(url, pages[url][1])
        if url == "https://mixed.example/post":
            return {"kind": "article", "url": url, "title": "Mixed", "text": "contradicts itself " * 30, "published_at": None,
                    "author": None, "outlet": "Mixed", "thumbnail": None, "links": []}
        if url == "https://weather.example/today":
            return {"kind": "article", "url": url, "title": "Sunny", "text": "unrelated weather " * 40, "published_at": None,
                    "author": None, "outlet": "Weather", "thumbnail": None, "links": []}
        from wassup.investigate.fetch import FetchError
        raise FetchError("not reachable in tests")

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "step_search", lambda conn, inv, llm: 0)  # no network
    client = TestClient(app)
    inv = client.post("/api/investigations", json={
        "title": "Pastor allegations", "brief": "What evidence exists?",
        "links": "https://news.example.com/2026/09/30/board-backs-pastor/?utm_source=x\nhttps://weather.example/today\nhttps://mixed.example/post"}).json()["id"]
    llm = FakeLLM()
    with db.connect() as conn:
        for _ in range(4):
            core.run_investigations(conn, llm=llm, max_seconds=30)
        core.step_summary(conn, inv, llm, force=True)
    d = client.get(f"/api/investigations/{inv}").json()
    by_url = {s["url"]: s for s in d["sources"]}
    article = by_url["https://news.example.com/2026/09/30/board-backs-pastor/"]
    assert article["status"] == "analyzed" and article["found_by"] == "you"
    assert by_url["https://weather.example/today"]["status"] == "unrelated"
    assert by_url["https://mixed.example/post"]["status"] == "analyzed"  # a contradictory answer keeps the source
    # The two links the article relies on were followed; the out of range number was ignored.
    traced = [s for s in d["sources"] if s["found_by"] == "traced"]
    assert {s["url"] for s in traced} == {"https://report.example/exclusive-pastor-documents", "https://files.example/letter.pdf"}
    assert all(s["parent_id"] == article["id"] and s["depth"] == 1 for s in traced)
    assert next(s for s in traced if s["url"].endswith(".pdf"))["kind"] == "document"
    assert all(s["status"] == "failed" for s in traced)  # unreachable here, and shown as such
    assert d["summary"]["headline"] and article["id"] in d["summary"]["source_ids"].values()
    # Pasting the text of a source replaces what could be read and sends it back to the model.
    r = client.post(f"/api/investigations/{inv}/text", json={"url": "https://report.example/exclusive-pastor-documents",
                                                            "text": "The full report text, pasted by hand. " * 10, "author": "Example Report"})
    assert r.status_code == 200
    d = client.get(f"/api/investigations/{inv}").json()
    rep = next(s for s in d["sources"] if s["url"] == "https://report.example/exclusive-pastor-documents")
    assert rep["status"] == "fetched" and rep["pasted"] and rep["chars"] > 100
    assert client.get(f"/api/investigations/{inv}/sources/{rep['id']}/text").json()["text"].startswith("The full report text")
    assert client.post(f"/api/investigations/{inv}/sources/{rep['id']}", json={"action": "hide"}).status_code == 200
    assert any(i["id"] == inv for i in client.get("/api/investigations").json())


@pytest.mark.usefixtures("database")
def test_investigator_leads_notes_and_hand_off(monkeypatch):
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.investigate import agent
    from wassup.newsroom import store
    from wassup.newsroom.paperclip import save_config

    client = TestClient(app)
    with db.connect() as conn:
        conn.execute("UPDATE investigations SET status = 'done'")  # from other tests
        conn.commit()
    inv = client.post("/api/investigations", json={"title": "Board statement", "brief": "Who said what, when?"}).json()["id"]
    # Not hired yet: asking says how to hire it.
    r = client.post(f"/api/investigations/{inv}/ask", json={"note": "check the church video"})
    assert r.status_code == 409 and "newsroom setup" in r.json()["detail"]

    issues = []

    class FakePaperclip:
        def create_issue(self, company_id, title, description, assignee, **kw):
            issues.append({"title": title, "description": description, "assignee": assignee, **kw})
            return {"id": f"issue-{len(issues)}"}

    monkeypatch.setattr(agent, "board", lambda conn: FakePaperclip())
    with db.connect() as conn:
        save_config(conn, board_token="t", company_id="c1", project_id="p1")
        store.upsert_agent(conn, "investigator", "investigator", "Investigator", paperclip_agent_id="ag-inv", status="active")
        conn.commit()
        assert agent.investigator_ready(conn)
        assert agent.hand_off(conn) == 0  # nothing read yet, nobody asked
    assert client.get(f"/api/investigations/{inv}").json()["investigator"] is True

    assert client.post(f"/api/investigations/{inv}/ask", json={"note": "check the church video"}).status_code == 200
    with db.connect() as conn:
        assert agent.hand_off(conn) == 1
        assert agent.hand_off(conn) == 0  # not twice while it works
    assert issues[0]["assignee"] == "ag-inv" and issues[0]["priority"] == "high"
    assert f"/api/investigations/{inv}/brief" in issues[0]["description"] and "check the church video" in issues[0]["description"]

    # What the Investigator does through the API.
    lead = client.post(f"/api/investigations/{inv}/leads", json={"title": "Church livestream archive", "why": "The board spoke at a service"}).json()["id"]
    assert client.post(f"/api/investigations/{inv}/leads/{lead}", json={"status": "done", "finding": "Service video found",
                                                                       "urls": ["https://video.example/sept-27"]}).status_code == 200
    assert client.post(f"/api/investigations/{inv}/leads/{lead}", json={"status": "solved"}).status_code == 400
    assert client.post(f"/api/investigations/{inv}/links", json={"links": "https://video.example/sept-27", "found_by": "investigator"}).json()["added"] == 1
    client.post(f"/api/investigations/{inv}/memo", json={"body": "## Checked\n- The service video shows the statement."})
    d = client.get(f"/api/investigations/{inv}").json()
    assert d["memo"].startswith("## Checked") and d["ask_note"] is None
    assert d["leads"][0]["status"] == "done" and d["leads"][0]["urls"] == ["https://video.example/sept-27"]
    assert next(s for s in d["sources"] if s["url"] == "https://video.example/sept-27")["found_by"] == "investigator"
    text = client.get(f"/api/investigations/{inv}/brief").text
    assert "Who said what, when?" in text and "Church livestream archive" in text and "Your notes from last time" in text
    # Your own lead, for the Investigator to pick up.
    assert client.post(f"/api/investigations/{inv}/leads", json={"title": "Ask about the district's report", "added_by": "you"}).status_code == 200
    assert any(l["added_by"] == "you" for l in client.get(f"/api/investigations/{inv}/leads").json())
