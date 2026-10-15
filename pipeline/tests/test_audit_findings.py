"""Fixes from the audits: desks need a keyword, not-news pages go cold, same-event stories merge,
stale and rejected connections come down, and audits explain and act on connections."""
from datetime import datetime, timedelta, timezone

import pytest

from wassup.triage.decider import StoryContext
from wassup.triage.rules import RulesDecider


def ctx(title, titles=(), countries=("US",), themes=("EPU_POLICY_GOVERNMENT", "TAX_FNCACT_PRESIDENT"), items=1):
    return StoryContext(id=1, title=title, item_titles=list(titles) or [title], summaries=[], urls=["https://e.com/1"],
                        themes=list(themes), countries=list(countries), item_count=items, source_count=1, country_count=1, tiers=["B"])


def test_a_desk_needs_one_of_its_words():
    r = RulesDecider()
    d = r.decide(ctx("Monster Jam returns to New Orleans for 35th anniversary"))
    assert not d.routed and d.confidence < 0.6  # placed in the US with government tags: unsure, not US politics
    d = r.decide(ctx("Senate vote on the government shutdown fails as Republicans split"))
    assert d.routed and d.desk == "us_politics"


def test_pages_that_are_not_news_go_cold():
    r = RulesDecider()
    for t in ("Chubb (NYSE:CB) Given New $350.00 Price Target at Morgan Stanley", "Terms of Service - Observer Today",
              "International News Briefs for Tuesday, October 6, 2026", "10-05-2026 KUAC MORNING NEWS"):
        d = r.decide(ctx(t))
        assert not d.routed and d.excluded_reason == "not_news", t
    # A live blog on one subject is news.
    assert r.decide(ctx("Live updates: Russia strikes Kyiv as Zelensky meets allies", countries=("UA",))).routed


def _two_stories(conn, tag):
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items

    now = datetime.now(timezone.utc)
    sid = ensure_source(conn, "findings-test", "Findings test", "rss")
    store_items(conn, sid, [
        RawItem(url=f"https://d.example/{tag}1", title="UK police arrest British man over RAF Fairford terror plot", published_at=now - timedelta(hours=3)),
        RawItem(url=f"https://d.example/{tag}2", title="Seventh arrest in Iran-linked plot near US air base in England", published_at=now - timedelta(hours=2)),
    ])
    conn.commit()
    process_new(conn)
    a = conn.execute("SELECT story_id FROM items WHERE url = %s", (f"https://d.example/{tag}1",)).fetchone()["story_id"]
    b = conn.execute("SELECT story_id FROM items WHERE url = %s", (f"https://d.example/{tag}2",)).fetchone()["story_id"]
    assert a != b
    return min(a, b), max(a, b)


@pytest.mark.usefixtures("database")
def test_same_event_stories_are_merged(monkeypatch):
    from wassup import db, dedupe
    from wassup.cluster import merge_stories

    with db.connect() as conn:
        a, b = _two_stories(conn, "dup")
        monkeypatch.setattr(dedupe, "candidates", lambda conn, limit=40: [(0.72, a, b)])
        asked = []

        def llm(prompt, schema):
            asked.append(prompt)
            return {"pairs": [{"n": 1, "answer": "same_story"}]}

        assert dedupe.run_duplicates(conn, llm=llm) == 1
        assert "RAF Fairford" in asked[0] and "Seventh arrest" in asked[0]
        merge_stories(conn)
        assert conn.execute("SELECT count(DISTINCT story_id) AS n FROM items WHERE url LIKE 'https://d.example/dup%%'").fetchone()["n"] == 1
        assert not conn.execute("SELECT 1 FROM story_pair_checks WHERE a = %s AND b = %s", (a, b)).fetchone()


@pytest.mark.usefixtures("database")
def test_connections_are_cleaned_and_audits_act_on_them():
    from psycopg.types.json import Jsonb

    from wassup import db
    from wassup.quality.audit import apply_verdict, line
    from wassup.quality.report import audit_report
    from wassup.signals import update_links

    with db.connect() as conn:
        a, b = _two_stories(conn, "lnk")
        conn.execute("UPDATE stories SET routed = true, desk = 'world_watch' WHERE id IN (%s, %s)", (a, b))
        # A stale link the rules no longer find, and one an agent drew.
        conn.execute("""INSERT INTO story_links (a, b, kind, weight, evidence, updated_at) VALUES (%s, %s, 'same_actor', 0.5, %s, now() - interval '1 day')
                        ON CONFLICT DO NOTHING""", (a, b, Jsonb({"entities": ["Somebody", "Someone Else"]})))
        conn.commit()
        update_links(conn)
        assert not conn.execute("SELECT 1 FROM story_links WHERE a = %s AND b = %s AND kind = 'same_actor'", (a, b)).fetchone()

        # An audit check on a link shows why it exists; "wrong" takes it down for good, "same" merges.
        subj = {"a": a, "b": b, "kind": "related", "a_title": "A", "b_title": "B", "by": "rule", "evidence": {"similarity": 0.66}}
        row = conn.execute("INSERT INTO audit_checks (aspect, story_id, subject) VALUES ('link', %s, %s) RETURNING *", (a, Jsonb(subj))).fetchone()
        assert "close in meaning (0.66)" in line(row)
        conn.execute("INSERT INTO story_links (a, b, kind, weight, evidence) VALUES (%s, %s, 'related', 0.66, '{}') ON CONFLICT DO NOTHING", (a, b))
        assert apply_verdict(conn, row, "wrong", "", "only share a word", "claude")
        assert not conn.execute("SELECT 1 FROM story_links WHERE a = %s AND b = %s", (a, b)).fetchone()
        assert conn.execute("SELECT 1 FROM link_rejections WHERE a = %s AND b = %s", (a, b)).fetchone()
        row2 = conn.execute("INSERT INTO audit_checks (aspect, story_id, subject) VALUES ('link', %s, %s) RETURNING *", (a, Jsonb(subj))).fetchone()
        apply_verdict(conn, row2, "wrong", "same", "one event", "claude")
        assert conn.execute("SELECT same FROM story_pair_checks WHERE a = %s AND b = %s", (a, b)).fetchone()["same"]
        conn.commit()
        report = audit_report(conn, 7)
        assert "## Connections" in report and "related" in report


def test_judge_slips_are_not_counted_as_wassup_mistakes():
    from wassup.quality.audit import clean_answer

    keys = {"us_politics", "russia_ukraine", "shipping", "migration"}
    # From a real audit: "wrong", but the desk it is already on.
    assert clean_answer("desk", "us_politics", {"desk": "us_politics"}, keys) == ("us_politics", "right")
    # Every question answered at once: only this check's own part counts.
    assert clean_answer("importance", "desk: none, missed: migration, place: none, importance: too_high, link: none", {}, keys) == ("too_high", None)
    # An answer to a different question is not a verdict on this one.
    assert clean_answer("importance", "desk: migration", {}, keys)[1] == "unsure"
    assert clean_answer("desk", "technology", {"desk": "shipping"}, keys)[1] == "unsure"
    assert clean_answer("missed", "none", {}, keys) == ("", "right")


@pytest.mark.usefixtures("database")
def test_local_judge_sees_one_kind_of_check_at_a_time():
    from psycopg.types.json import Jsonb

    from wassup import db
    from wassup.quality.run import judge_locally

    with db.connect() as conn:
        aid = conn.execute("INSERT INTO audits (judge) VALUES ('local') RETURNING id").fetchone()["id"]
        for aspect, subj in (("importance", {"title": "T1", "band": "high", "significance": 3.0, "articles": 3, "outlets": 2, "countries": 1}),
                             ("link", {"a": 1, "b": 2, "kind": "related", "a_title": "A", "b_title": "B"})):
            conn.execute("INSERT INTO audit_checks (audit_id, aspect, subject) VALUES (%s, %s, %s)", (aid, aspect, Jsonb(subj)))
        conn.commit()
        prompts = []

        def llm(prompt, schema):
            prompts.append((prompt, schema))
            ids = [int(l[1:l.index("]")]) for l in prompt.splitlines() if l.startswith("[")]
            return {"verdicts": [{"id": i, "verdict": "right", "answer": "", "note": ""} for i in ids]}

        judge_locally(conn, aid, llm=llm)
        conn.execute("DELETE FROM audits WHERE id = %s", (aid,))  # one audit a day: leave the day free for other tests
        conn.commit()
    assert len(prompts) == 2
    imp = next(p for p in prompts if "] importance |" in p[0])
    assert "] link |" not in imp[0]
    assert imp[1]["properties"]["verdicts"]["items"]["properties"]["answer"]["enum"] == ["", "too_high", "too_low"]


@pytest.mark.usefixtures("database")
def test_rescore_undoes_a_muddled_verdict():
    from psycopg.types.json import Jsonb

    from wassup import db
    from wassup.quality.audit import rescore

    with db.connect() as conn:
        a, _ = _two_stories(conn, "rsc")
        # The old reading of "desk: russia_ukraine" sent the story to cold storage.
        conn.execute("UPDATE stories SET routed = false, desk = 'shipping', excluded_reason = 'desk_review_none' WHERE id = %s", (a,))
        conn.execute("""INSERT INTO audit_checks (aspect, story_id, subject, verdict, answer, fixed, by, judged_at)
                        VALUES ('desk', %s, %s, 'wrong', 'desk: russia_ukraine', true, 'local', now())""", (a, Jsonb({"desk": "shipping"})))
        conn.commit()
        r = rescore(conn)
        assert r["moved"] == 1
        s = conn.execute("SELECT routed, desk FROM stories WHERE id = %s", (a,)).fetchone()
        assert s["routed"] and s["desk"] == "russia_ukraine"  # what the judge meant
        conn.execute("DELETE FROM audit_checks WHERE story_id = %s", (a,))  # not one of your reports
        conn.commit()


def test_a_summary_that_repeats_its_instruction_is_caught():
    from wassup.newsroom.desk import echoes_instructions

    for t in ("Two to four sentences on what is happening on your desk right now.",
              "Two to four sentences summarizing the key developments.", ""):
        assert echoes_instructions(t), t
    assert not echoes_instructions("Saudi-backed Yemeni forces expelled the Houthis from areas near the Bab el-Mandeb Strait.")


@pytest.mark.usefixtures("database")
def test_retriage_queues_every_story_in_batches():
    from wassup import db
    from wassup.triage import queue_all

    with db.connect() as conn:
        _two_stories(conn, "rtq")
        conn.execute("UPDATE stories SET triaged_item_count = 3")
        conn.commit()
        n = queue_all(conn, chunk=1)
        assert n >= 2 and not conn.execute("SELECT 1 FROM stories WHERE triaged_item_count <> 0").fetchone()
