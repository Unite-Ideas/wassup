"""Accuracy audits: how often Wassup classifies stories correctly, measured, not guessed.

Every night a random sample of the last day's decisions is checked:
- desk: is this story on the right desk? (a few per desk)
- missed: should this story in cold storage be on a desk?
- place: is the story's main place right?
- grouping: do all these articles belong in one story?
- importance: is the story ranked about right?
- link: is this connection between two stories real?

The judge is the Standards Editor, a Paperclip agent on Claude (standards_instructions.md), when
it is hired; otherwise the local model. A wrong desk, a missed story or a wrong place is fixed
on the spot (the story says the audit fixed it); a wrong grouping takes the stray articles out;
importance and connections are only measured for now. Your own reports from the story panel are
counted the same way, judged by you. The QUALITY tab shows the error rate of each over time.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..config import load_yaml
from ..triage.rules import load_desks

log = logging.getLogger(__name__)

ASPECTS = ["desk", "missed", "place", "grouping", "importance", "link"]
DEFAULTS = {"per_desk": 6, "missed": 20, "place": 25, "grouping": 10, "importance": 15, "link": 10, "hours": 24,
            "every_hours": 22, "judge": "auto"}


def cfg() -> dict:
    c = load_yaml("quality.yaml") or {}
    return {k: c.get(k, v) for k, v in DEFAULTS.items()}


def _band(sig: float) -> str:
    return "top" if sig >= 4 else "high" if sig >= 3 else "medium" if sig >= 2 else "low"


def _headlines(conn, story_id: int, n: int = 3) -> list[str]:
    return [r["t"] for r in conn.execute(
        "SELECT coalesce(title_en, title) AS t FROM items WHERE story_id = %s ORDER BY published_at LIMIT %s", (story_id, n))]


def sample(conn: psycopg.Connection, audit_id: int, c: dict | None = None) -> int:
    """Pick tonight's checks and store them with what was decided."""
    c = c or cfg()
    hours = c["hours"]
    rnd = random.Random(audit_id)
    checks: list[tuple[str, int | None, dict]] = []
    desks = {d.key: d for d in load_desks()}
    recent = f"s.last_seen > now() - interval '{int(hours)} hours'"
    for key in desks:
        for s in conn.execute(f"""SELECT s.id, coalesce(s.title_en, s.title) AS title FROM stories s
                                  WHERE {recent} AND s.routed AND s.desk = %s ORDER BY random() LIMIT %s""", (key, c["per_desk"])):
            checks.append(("desk", s["id"], {"title": s["title"], "desk": key, "headlines": _headlines(conn, s["id"])}))
    for s in conn.execute(f"""SELECT s.id, coalesce(s.title_en, s.title) AS title, s.excluded_reason FROM stories s
                              WHERE {recent} AND NOT s.routed AND s.item_count >= 2
                                AND coalesce(s.excluded_reason, 'no_desk_match') IN ('no_desk_match', 'desk_review_none')
                              ORDER BY random() LIMIT %s""", (c["missed"],)):
        checks.append(("missed", s["id"], {"title": s["title"], "reason": s["excluded_reason"], "headlines": _headlines(conn, s["id"])}))
    for s in conn.execute(f"""SELECT s.id, coalesce(s.title_en, s.title) AS title, p.name, p.country, p.kind FROM stories s
                              JOIN places p ON p.id = s.primary_place_id
                              WHERE {recent} AND s.routed ORDER BY random() LIMIT %s""", (c["place"],)):
        checks.append(("place", s["id"], {"title": s["title"], "place": s["name"], "country": s["country"], "kind": s["kind"],
                                          "headlines": _headlines(conn, s["id"])}))
    for s in conn.execute(f"""SELECT s.id, coalesce(s.title_en, s.title) AS title FROM stories s
                              WHERE {recent} AND s.routed AND s.item_count >= 4 ORDER BY random() LIMIT %s""", (c["grouping"],)):
        arts = conn.execute("""SELECT id, coalesce(title_en, title) AS t, outlet FROM items WHERE story_id = %s
                               ORDER BY random() LIMIT 8""", (s["id"],)).fetchall()
        checks.append(("grouping", s["id"], {"title": s["title"], "articles": [{"id": a["id"], "title": a["t"], "outlet": a["outlet"]} for a in arts]}))
    for s in conn.execute(f"""SELECT s.id, coalesce(s.title_en, s.title) AS title, s.significance, s.item_count, s.source_count,
                                     s.country_count, s.desk FROM stories s
                              WHERE {recent} AND s.routed ORDER BY random() LIMIT %s""", (c["importance"],)):
        checks.append(("importance", s["id"], {"title": s["title"], "band": _band(s["significance"]), "significance": s["significance"],
                                               "articles": s["item_count"], "outlets": s["source_count"], "countries": s["country_count"]}))
    for l in conn.execute(f"""SELECT l.a, l.b, l.kind, l.evidence, l.created_by, coalesce(sa.title_en, sa.title) AS ta,
                                     coalesce(sb.title_en, sb.title) AS tb
                              FROM story_links l JOIN stories sa ON sa.id = l.a JOIN stories sb ON sb.id = l.b
                              WHERE sa.last_seen > now() - interval '{int(hours)} hours' ORDER BY random() LIMIT %s""", (c["link"],)):
        checks.append(("link", l["a"], {"a": l["a"], "b": l["b"], "kind": l["kind"], "a_title": l["ta"], "b_title": l["tb"],
                                        "by": l["created_by"], "evidence": (l["evidence"] or "")[:200] if isinstance(l["evidence"], str) else l["evidence"]}))
    rnd.shuffle(checks)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO audit_checks (audit_id, aspect, story_id, subject) VALUES (%s, %s, %s, %s)",
                        [(audit_id, a, sid, Jsonb(subj)) for a, sid, subj in checks])
    return len(checks)


def header() -> list[str]:
    return ["## Desks", *[f"- {d.key}: {d.name}. {d.description}" for d in load_desks()], ""]


def line(r: dict) -> str:
    """One check as the judge reads it."""
    s = r["subject"]
    heads = "".join(f"\n    also: {h}" for h in (s.get("headlines") or [])[1:3])
    if r["aspect"] == "desk":
        return f"[{r['id']}] desk | on desk `{s['desk']}` | {s['title']}{heads}"
    if r["aspect"] == "missed":
        return f"[{r['id']}] missed | in cold storage (on no desk) | {s['title']}{heads}"
    if r["aspect"] == "place":
        return f"[{r['id']}] place | placed at {s['place']} ({s['country']}, {s['kind']}) | {s['title']}{heads}"
    if r["aspect"] == "grouping":
        arts = "".join(f"\n    ({i + 1}) {a['title']} [{a['outlet'] or ''}]" for i, a in enumerate(s["articles"]))
        return f"[{r['id']}] grouping | story: {s['title']}{arts}"
    if r["aspect"] == "importance":
        return (f"[{r['id']}] importance | ranked {s['band']} ({s['significance']:.1f} of 5; {s['articles']} articles, "
                f"{s['outlets']} outlets, {s['countries']} countries) | {s['title']}")
    return f"[{r['id']}] link | `{s['kind']}` between: (A) {s['a_title']} and (B) {s['b_title']}"


def sheet(conn: psycopg.Connection, audit_id: int) -> str:
    """The checks still to judge, as one text."""
    rows = conn.execute("SELECT * FROM audit_checks WHERE audit_id = %s AND verdict IS NULL ORDER BY aspect, id", (audit_id,)).fetchall()
    return "\n".join([f"# Audit {audit_id}: {len(rows)} checks", "", *header(), *(line(r) for r in rows), "", GUIDE])


GUIDE = """For each check give a verdict: right, wrong or unsure (unsure only when the headlines cannot tell).
When wrong, answer with what is right:
- desk: the right desk key, or none
- missed: the desk key it belongs on (it was wrongly left out), or nothing if right to leave out
- place: the right place as "City, CC" or "Country, CC" (CC the ISO 3166 two letter code), or none
- grouping: the numbers of the articles that do not belong, like "2, 5"
- importance: too_high or too_low (top means among the day's biggest stories, low means minor)
- link: nothing (just wrong)
Judge by what happened, not by words a story shares with a desk or a place. The country of the newspaper is not
the story's place. A story is on the right desk if it is clearly within that desk's subject."""


def apply_verdict(conn: psycopg.Connection, check: dict, verdict: str, answer: str | None, note: str | None, by: str) -> bool:
    """Record a verdict and fix what can be fixed. Returns whether something was changed."""
    verdict = verdict if verdict in ("right", "wrong", "unsure") else "unsure"
    answer = (answer or "").strip()
    fixed = False
    s = check["subject"]
    sid = check["story_id"]
    keys = {d.key for d in load_desks()}
    if verdict == "wrong" and sid:
        if check["aspect"] in ("desk", "missed"):
            desk = answer if answer in keys else None
            if desk or check["aspect"] == "desk":
                _set_desk(conn, sid, desk, by)
                fixed = True
        elif check["aspect"] == "place" and answer and answer.lower() != "none":
            fixed = _set_place(conn, sid, answer, by)
        elif check["aspect"] == "grouping" and answer:
            from ..cluster import detach_item
            arts = s.get("articles") or []
            for n in {int(x) for x in answer.replace(",", " ").split() if x.isdigit()}:
                if 1 <= n <= len(arts):
                    detach_item(conn, arts[n - 1]["id"])
                    fixed = True
    conn.execute("""UPDATE audit_checks SET verdict = %s, answer = %s, note = %s, fixed = %s, by = %s, judged_at = now()
                    WHERE id = %s""", (verdict, answer[:300] or None, (note or "")[:500] or None, fixed, by, check["id"]))
    return fixed


def _set_desk(conn, story_id: int, desk: str | None, by: str) -> None:
    review = {"desk": desk, "sure": True, "by": by, "at": datetime.now(timezone.utc).isoformat()}
    conn.execute("""UPDATE stories SET desk = coalesce(%s, desk), routed = %s, excluded_reason = %s, desk_review = coalesce(desk_review, '{}'::jsonb) || %s,
                    desk_reviewed_items = item_count, updated_at = now() WHERE id = %s""",
                 (desk, desk is not None, None if desk else "desk_review_none", Jsonb(review), story_id))


def _set_place(conn, story_id: int, answer: str, by: str) -> bool:
    from ..collectors.base import _place_id
    from ..geo import gazetteer
    from ..locate import set_location

    name, _, cc = answer.rpartition(",")
    name, cc = (name or answer).strip(), cc.strip().upper()
    g = gazetteer()
    p = g.by_name(name)
    if p is None or (len(cc) == 2 and p.country != cc):
        p = g.country(cc) if len(cc) == 2 else None
    if p is None:
        return False
    set_location(conn, story_id, _place_id(conn, p, {}), f"audit:{by}", 0.9, record=True)
    return True


def scorecard(conn: psycopg.Connection, days: int = 30) -> dict:
    audits = conn.execute(
        """SELECT a.id, a.judge, a.status, a.created_at, a.finished_at,
                  jsonb_object_agg(x.aspect, jsonb_build_object('checked', x.checked, 'wrong', x.wrong, 'unsure', x.unsure)) AS aspects
           FROM audits a JOIN (SELECT audit_id, aspect, count(*) FILTER (WHERE verdict IS NOT NULL) AS checked,
                                      count(*) FILTER (WHERE verdict = 'wrong') AS wrong, count(*) FILTER (WHERE verdict = 'unsure') AS unsure
                               FROM audit_checks WHERE audit_id IS NOT NULL GROUP BY 1, 2) x ON x.audit_id = a.id
           WHERE a.created_at > now() - %s * interval '1 day' GROUP BY a.id ORDER BY a.id""", (days,)).fetchall()
    reports = conn.execute(
        """SELECT aspect, count(*) AS n FROM audit_checks WHERE audit_id IS NULL AND judged_at > now() - %s * interval '1 day'
           GROUP BY 1""", (days,)).fetchall()
    mistakes = conn.execute(
        """SELECT c.id, c.audit_id, c.aspect, c.story_id, c.subject, c.answer, c.note, c.fixed, c.by, c.judged_at,
                  coalesce(s.title_en, s.title) AS story_title
           FROM audit_checks c LEFT JOIN stories s ON s.id = c.story_id
           WHERE c.verdict = 'wrong' AND c.judged_at > now() - %s * interval '1 day'
           ORDER BY c.judged_at DESC LIMIT 60""", (days,)).fetchall()
    return {"audits": audits, "reports": {r["aspect"]: r["n"] for r in reports}, "mistakes": mistakes, "aspects": ASPECTS}
