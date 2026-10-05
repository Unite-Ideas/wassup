"""Stories that turn out to be the same event are merged, taking their newsroom work along."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.collectors.base import RawItem, ensure_source, store_items
from wassup.text import comparable_title, is_junk_title


def test_comparable_title_drops_wire_labels_only():
    assert comparable_title("(LEAD) (Asiad) Kim wins gold") == "Kim wins gold"
    assert comparable_title("UPDATE 2-Oil rises on supply fears") == "Oil rises on supply fears"
    assert comparable_title("BREAKING: Quake hits Taiwan") == "Quake hits Taiwan"
    assert comparable_title("US-China talks: what to know") == "US-China talks: what to know"
    assert comparable_title("Update on the war: day 900") == "Update on the war: day 900"


def test_junk_pages_are_recognised():
    for t in ("Terms of Service - The Times Leader", "Subscribe to the Cairns Post", "Health News - Glenora Radio Network",
              "Senior Full-Stack Engineer", "Latest Articles"):
        assert is_junk_title(t), t
    for t in ("Fox News hires Karoline Leavitt", "Breaking News: quake hits Taiwan", "Loud explosions heard in Yemen capital"):
        assert not is_junk_title(t), t


@pytest.mark.usefixtures("database")
def test_duplicate_stories_merge():
    from wassup import cluster, db
    from wassup.db import kv_set

    now = datetime.now(timezone.utc)
    def it(n, title):
        return RawItem(url=f"https://merge.example/{n}", title=title, published_at=now - timedelta(minutes=n), outlet=f"m{n}.com")
    with db.connect() as conn:
        sid = ensure_source(conn, "merge-test", "Merge test", "rss")
        store_items(conn, sid, [it(1, "Zanzibar ferry operator halts sailings after engine fire"),
                                it(2, "Zanzibar ferry operator halts sailings after an engine fire"),
                                it(3, "Glacier tour company in Patagonia suspends trips over safety audit"),
                                it(4, "Glacier tour company in Patagonia suspends its trips over a safety audit")])
        conn.commit()
        cluster.process_new(conn)
        a, b = (conn.execute("SELECT story_id FROM items WHERE url = %s", (f"https://merge.example/{n}",)).fetchone()["story_id"]
                for n in (1, 3))
        assert a != b
        kv_set(conn, "merge_since", conn.execute("SELECT now()::text AS t").fetchone()["t"])
        # Pretend the two turned out to be the same event, and give one of them newsroom work.
        conn.execute("UPDATE stories SET centroid = (SELECT centroid FROM stories WHERE id = %s), updated_at = now() + interval '1 second' "
                     "WHERE id IN (%s, %s)", (a, a, b))
        conn.execute("INSERT INTO briefs (kind, story_id, agent_key, body) VALUES ('story', %s, 'desk:test', 'brief')", (b,))
        conn.execute("INSERT INTO feedback (story_id, value) VALUES (%s, 1)", (b,))
        conn.commit()
        cluster._index.loaded_at = 0

        assert cluster.merge_stories(conn) >= 1
        keep = conn.execute("SELECT DISTINCT story_id FROM items WHERE url LIKE 'https://merge.example/%'").fetchall()
        assert len(keep) == 1
        k = keep[0]["story_id"]
        gone = b if k == a else a
        s = conn.execute("SELECT item_count, triaged_item_count FROM stories WHERE id = %s", (k,)).fetchone()
        assert s["item_count"] == 4 and s["triaged_item_count"] == 0  # feedback came along, so triage again
        assert conn.execute("SELECT count(*) n FROM stories WHERE id = %s", (gone,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) n FROM briefs WHERE story_id = %s", (k,)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) n FROM feedback WHERE story_id = %s", (k,)).fetchone()["n"] == 1
