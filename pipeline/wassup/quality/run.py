"""Running audits: once a day (or when you press RUN AN AUDIT NOW), pick the sample and have it
judged. The Standards Editor (Claude, through Paperclip) judges when it is hired and
`judge` in config/quality.yaml allows; otherwise the local model, a batch at a time. An audit
the Standards Editor has not finished in eight hours is finished by the local model.
"""
from __future__ import annotations

import logging
import os
import time

import psycopg

from ..db import kv_get, kv_set
from ..newsroom import store
from ..newsroom.paperclip import PaperclipError, board, config
from . import audit

log = logging.getLogger(__name__)

BATCH = 10
SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["right", "wrong", "unsure"]},
        "answer": {"type": "string"}, "note": {"type": "string"}}, "required": ["id", "verdict", "answer", "note"]}}},
    "required": ["verdicts"],
}


def standards_ready(conn) -> bool:
    row = store.get_agent(conn, "standards")
    pcfg = config(conn)
    return bool(row and row["status"] == "active" and row["paperclip_agent_id"] and pcfg.get("board_token") and pcfg.get("company_id"))


def start(conn: psycopg.Connection, c: dict | None = None) -> int:
    c = c or audit.cfg()
    use_claude = c["judge"] in ("auto", "claude") and standards_ready(conn)
    aid = conn.execute("INSERT INTO audits (judge) VALUES (%s) RETURNING id", ("claude" if use_claude else "local",)).fetchone()["id"]
    n = audit.sample(conn, aid, c)
    conn.commit()
    log.info("audit %s: %d checks, judged by %s", aid, n, "the Standards Editor" if use_claude else "the local model")
    if use_claude:
        try:
            agent = store.get_agent(conn, "standards")
            pcfg = config(conn)
            base = os.environ.get("WASSUP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
            issue = board(conn).create_issue(
                pcfg["company_id"], f"Accuracy audit #{aid} ({n} checks)",
                f"""Tonight's accuracy audit: {n} of Wassup's decisions from the last day, picked at random.

Read the checks with `curl -s "$WASSUP_URL/api/audits/{aid}/sheet"`, judge each one as your instructions say,
send your verdicts, then finish the audit and close this issue. The scorecard is in the app's QUALITY tab: {base}""",
                agent["paperclip_agent_id"], project_id=pcfg.get("project_id"))
            conn.execute("UPDATE audits SET issue_id = %s WHERE id = %s", (issue.get("id"), aid))
        except PaperclipError as e:
            log.warning("audit %s: could not reach the Standards Editor (%s); the local model judges it", aid, e)
            conn.execute("UPDATE audits SET judge = 'local' WHERE id = %s", (aid,))
        conn.commit()
    return aid


QUESTION = {
    "desk": "is the story on the right desk? A desk is right if the story is clearly within that desk's subject.",
    "missed": "this story is on no desk (cold storage). Should it be on one?",
    "place": "is the story's main place right? Its place is where it happens or what it is about, not the newspaper's country.",
    "grouping": "do all these articles report the same event or development?",
    "importance": "is the story ranked about right? Top means among the day's biggest stories worldwide, low means minor or local.",
    "link": "is this connection between two stories useful: would a reader of one want to know about the other?",
}
ANSWER = {
    "desk": "the desk key it belongs on, or none",
    "missed": "the desk key it belongs on",
    "place": "the right place as City, CC or Country, CC (CC the two letter country code), or none",
    "grouping": "the numbers of the articles that do not belong, like 2, 5",
    "importance": "too_high or too_low",
    "link": "same if the two are the same event (they should be one story), else leave empty",
}


def schema_for(aspect: str) -> dict:
    """The judge's answer limited to what fits this check's question."""
    from ..triage.rules import load_desks

    keys = [d.key for d in load_desks()]
    answer = {"desk": {"type": "string", "enum": ["", "none", *keys]}, "missed": {"type": "string", "enum": ["", *keys]},
              "importance": {"type": "string", "enum": ["", "too_high", "too_low"]},
              "link": {"type": "string", "enum": ["", "same"]}}.get(aspect, {"type": "string"})
    return {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["right", "wrong", "unsure"]},
        "answer": answer, "note": {"type": "string"}}, "required": ["id", "verdict", "answer", "note"]}}},
        "required": ["verdicts"]}


def judge_locally(conn: psycopg.Connection, aid: int, llm=None, max_seconds: float = 120) -> int:
    if llm is None:
        from ..newsroom.llm import default_llm
        llm = default_llm()
    started, done = time.time(), 0
    while time.time() - started < max_seconds:
        # One kind of check per batch, with only its own question, so the model cannot mix them up.
        nxt = conn.execute("SELECT aspect FROM audit_checks WHERE audit_id = %s AND verdict IS NULL ORDER BY aspect, id LIMIT 1",
                           (aid,)).fetchone()
        rows = conn.execute("SELECT * FROM audit_checks WHERE audit_id = %s AND verdict IS NULL AND aspect = %s ORDER BY id LIMIT %s",
                            (aid, nxt["aspect"], BATCH)).fetchall() if nxt else []
        if not rows:
            conn.execute("UPDATE audits SET status = 'done', finished_at = now() WHERE id = %s", (aid,))
            conn.commit()
            log.info("audit %s finished", aid)
            break
        aspect = rows[0]["aspect"]
        prompt = "\n".join(["You check a news intelligence system's decisions for accuracy.", "",
                             *(audit.header() if aspect in ("desk", "missed") else []),
                             f"Every check below asks one question: {QUESTION[aspect]}", "",
                             *(audit.line(r) for r in rows), "",
                             "For each check: verdict right, wrong or unsure (unsure only when the headlines cannot tell).",
                             f"When wrong, answer: {ANSWER[aspect]}. When right, leave answer empty.",
                             "Judge by what happened, not by words a story shares with a desk or a place. Answer for every check id above."])
        try:
            out = llm(prompt, schema_for(aspect))
        except Exception as e:
            log.warning("audit %s: local judge failed: %s", aid, e)
            break
        by_id = {r["id"]: r for r in rows}
        answered = 0
        for v in out.get("verdicts") or []:
            r = by_id.get(v.get("id"))
            if r:
                audit.apply_verdict(conn, r, v.get("verdict"), v.get("answer"), v.get("note"), "local")
                answered += 1
        for r in rows:  # anything the model skipped counts as unsure, so the audit moves on
            if r["id"] not in {v.get("id") for v in out.get("verdicts") or []}:
                audit.apply_verdict(conn, r, "unsure", None, "not answered", "local")
        conn.commit()
        done += len(rows)
    return done


def run_audits(conn: psycopg.Connection, llm=None, max_seconds: float = 120) -> int:
    if os.environ.get("AUDIT", "on") == "off":
        return 0
    c = audit.cfg()
    conn.execute("""UPDATE audits SET judge = 'local' WHERE status = 'open' AND judge = 'claude'
                    AND created_at < now() - interval '8 hours'""")
    conn.commit()
    last = conn.execute("SELECT max(created_at) AS t FROM audits").fetchone()["t"]
    asked = kv_get(conn, "audit_now")
    if asked or last is None or (time.time() - last.timestamp()) > c["every_hours"] * 3600:
        if asked:
            kv_set(conn, "audit_now", False)
        if not conn.execute("SELECT 1 FROM audits WHERE status = 'open' AND created_at > now() - interval '1 hour'").fetchone():
            start(conn, c)
    work = 0
    for a in conn.execute("SELECT id FROM audits WHERE status = 'open' AND judge = 'local' ORDER BY id").fetchall():
        work += judge_locally(conn, a["id"], llm, max_seconds)
    return work
