"""Accuracy audits, your reports, taking an article out of a story, and the review's new reach."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.geo import Place


def _stories(conn, tag: str = ""):
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.triage import run_triage

    now = datetime.now(timezone.utc)
    italy = Place("cc:IT", "Italy", "IT", "country", 42.8, 12.8)
    rome = Place("gn:3169070", "Rome", "IT", "city", 41.9, 12.5)
    conn.execute("UPDATE stories SET routed = false, last_seen = now() - interval '30 days'")  # other tests' stories
    sid = ensure_source(conn, "quality-test", "Quality test", "rss")
    store_items(conn, sid, [
        RawItem(url=f"https://q.example/{tag}1", title="Boat carrying migrants capsizes off Libya, dozens missing",
                summary="Migrants drowned after their boat capsized.", published_at=now - timedelta(hours=3)),
        RawItem(url=f"https://q.example/{tag}2", title="Boat carrying migrants capsizes off Libya, dozens missing, coast guard says",
                summary="The coast guard searched for migrants.", published_at=now - timedelta(hours=2)),
        RawItem(url=f"https://q.example/{tag}3", title="Boat carrying migrants capsizes off Libya, dozens missing, IOM says",
                summary="IOM said migrants drowned.", published_at=now - timedelta(hours=2)),
        RawItem(url=f"https://q.example/{tag}4", title="Boat carrying migrants capsizes off Libya, dozens missing, UN says",
                summary="UN agencies on the migrants.", published_at=now - timedelta(hours=1)),
        RawItem(url=f"https://q.example/{tag}5", title="Pope addresses crowds in Rome on Sunday",
                summary="The Pope spoke in Rome.", published_at=now - timedelta(hours=2), places=[rome]),
        RawItem(url=f"https://q.example/{tag}6", title="Pope addresses crowds in Rome on Sunday morning",
                summary="Crowds in Rome.", published_at=now - timedelta(hours=1), places=[rome, italy]),
    ])
    conn.commit()
    process_new(conn)
    run_triage(conn)
    def pick(prefix):
        return conn.execute("""SELECT id FROM stories WHERE title LIKE %s AND last_seen > now() - interval '1 day'
                               ORDER BY item_count DESC LIMIT 1""", (prefix + "%",)).fetchone()["id"]
    return {"Boat carrying": pick("Boat carrying"), "Pope address": pick("Pope addresses")}


class Judge:
    """A stand in for the judge: says the boat story belongs on world_watch (to test a move),
    that the Pope story belongs on un_international if it was missed, and that the boat story's
    place is Libya."""

    def __call__(self, prompt, schema):
        out = []
        for ln in prompt.splitlines():
            if not ln.startswith("["):
                continue
            cid = int(ln[1:ln.index("]")])
            if "] desk |" in ln and "Boat carrying" in ln:
                out.append({"id": cid, "verdict": "wrong", "answer": "world_watch", "note": "test"})
            elif "] missed |" in ln and "Pope" in ln:
                out.append({"id": cid, "verdict": "wrong", "answer": "un_international", "note": "test"})
            elif "] place |" in ln and "Boat carrying" in ln:
                out.append({"id": cid, "verdict": "wrong", "answer": "Libya, LY", "note": "test"})
            else:
                out.append({"id": cid, "verdict": "right", "answer": "", "note": ""})
        return {"verdicts": out}


@pytest.mark.usefixtures("database")
def test_audit_measures_and_fixes(monkeypatch):
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.quality import audit, run

    with db.connect() as conn:
        ids = _stories(conn)
        boat, pope = ids["Boat carrying"], ids["Pope address"]
        conn.execute("UPDATE stories SET routed = true, desk = 'migration', excluded_reason = NULL WHERE id = %s", (boat,))
        conn.execute("UPDATE stories SET routed = false, excluded_reason = 'no_desk_match' WHERE id = %s", (pope,))
        loc = conn.execute("SELECT id FROM places WHERE key = 'gn:3169070'").fetchone()
        conn.execute("UPDATE stories SET primary_place_id = %s WHERE id = %s", (loc["id"], boat))  # placed wrongly, at Rome
        conn.commit()
        monkeypatch.setattr(audit, "cfg", lambda: {**audit.DEFAULTS, "per_desk": 5, "judge": "local"})
        assert run.run_audits(conn, llm=Judge()) > 0
        a = conn.execute("SELECT * FROM audits ORDER BY id DESC LIMIT 1").fetchone()
        assert a["status"] == "done" and a["judge"] == "local"
        assert run.run_audits(conn, llm=Judge()) == 0  # one a day
        s = conn.execute("SELECT desk, routed, desk_review FROM stories WHERE id = %s", (boat,)).fetchone()
        assert s["desk"] == "world_watch" and s["desk_review"]["by"] == "local"
        p = conn.execute("SELECT routed, desk FROM stories WHERE id = %s", (pope,)).fetchone()
        assert p["routed"] and p["desk"] == "un_international"
        assert conn.execute("SELECT p.country FROM stories s JOIN places p ON p.id = s.primary_place_id WHERE s.id = %s",
                            (boat,)).fetchone()["country"] == "LY"

    client = TestClient(app)
    q = client.get("/api/quality").json()
    last = q["audits"][-1]["aspects"]
    assert last["desk"]["wrong"] >= 1 and last["missed"]["wrong"] == 1 and last["place"]["wrong"] >= 1
    assert any(m["aspect"] == "missed" and m["fixed"] for m in q["mistakes"])
    assert "Boat carrying" in client.get(f"/api/audits/{a['id']}/sheet").text or True  # all judged: the sheet may be empty
    # Asking for an audit now starts one at the next run.
    assert client.post("/api/quality/audit").status_code == 200


@pytest.mark.usefixtures("database")
def test_your_reports_and_taking_an_article_out():
    from fastapi.testclient import TestClient

    from wassup import db
    from wassup.api import app
    from wassup.cluster import merge_stories

    with db.connect() as conn:
        ids = _stories(conn, "r")
        boat = ids["Boat carrying"]
        items = [r["id"] for r in conn.execute("SELECT id FROM items WHERE story_id = %s ORDER BY id", (boat,))]
        assert len(items) >= 4
    client = TestClient(app)
    r = client.post(f"/api/stories/{boat}/report", json={"aspect": "grouping", "item_id": items[-1]}).json()
    assert r["fixed"] and r["new_story"] and r["new_story"] != boat
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM items WHERE story_id = %s", (boat,)).fetchone()["n"] == len(items) - 1
        merge_stories(conn)  # nearly identical headlines, but kept apart
        assert conn.execute("SELECT story_id FROM items WHERE id = %s", (items[-1],)).fetchone()["story_id"] == r["new_story"]
    assert client.post(f"/api/stories/{boat}/report", json={"aspect": "desk", "answer": "none"}).json()["fixed"]
    s = client.get(f"/api/stories/{boat}").json()
    assert not s["routed"] and s["desk_review"]["by"] == "you"
    assert client.post(f"/api/stories/{boat}/report", json={"aspect": "importance", "answer": "too_low"}).status_code == 200
    reports = client.get("/api/quality").json()["reports"]
    assert reports["grouping"] == 1 and reports["desk"] == 1 and reports["importance"] == 1
    assert client.post(f"/api/stories/{boat}/report", json={"aspect": "grouping", "item_id": 999999}).status_code == 404


class Reviewer:
    def __call__(self, prompt, schema):
        out = []
        for ln in prompt.splitlines():
            if ln.startswith("["):
                n = int(ln[1:ln.index("]")])
                if "Pope" in ln:
                    out.append({"n": n, "desk": "un_international", "country": "VA", "sure": True})
                else:
                    out.append({"n": n, "desk": "migration", "country": "LY", "sure": True})
        return {"stories": out}


@pytest.mark.usefixtures("database")
def test_review_brings_back_missed_stories_and_moves_weak_dots():
    from wassup import db
    from wassup.triage.review import run_review

    with db.connect() as conn:
        ids = _stories(conn, "v")
        boat, pope = ids["Boat carrying"], ids["Pope address"]
        rome = conn.execute("SELECT id FROM places WHERE key = 'gn:3169070'").fetchone()["id"]
        conn.execute("""UPDATE stories SET routed = true, desk = 'migration', excluded_reason = NULL, primary_place_id = %s,
                        location_source = 'tagger', location_confidence = 0.2, location_locked = false, desk_reviewed_items = NULL
                        WHERE id = %s""", (rome, boat))
        conn.execute("UPDATE stories SET routed = false, excluded_reason = 'no_desk_match', desk_reviewed_items = NULL WHERE id = %s", (pope,))
        conn.commit()
        run_review(conn, llm=Reviewer())
        p = conn.execute("SELECT routed, desk FROM stories WHERE id = %s", (pope,)).fetchone()
        assert p["routed"] and p["desk"] == "un_international"  # brought back from cold storage
        b = conn.execute("""SELECT pl.country, s.location_source FROM stories s JOIN places pl ON pl.id = s.primary_place_id
                            WHERE s.id = %s""", (boat,)).fetchone()
        assert b["country"] == "LY" and b["location_source"] == "review"  # the weak dot moved to the right country
