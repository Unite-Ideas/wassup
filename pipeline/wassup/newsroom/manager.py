"""The newsroom's moving parts that run inside the pipeline (no AI here):

  * escalate: hand breaking stories, and stories a desk flagged, to the Editor in Chief as a
    Paperclip issue, each story once, within a daily cap;
  * standups: when one is called, wake every desk, collect their reports, and hand them to
    the Editor in Chief to write up.
"""
from __future__ import annotations

import logging
import os

import psycopg

from ..config import load_yaml
from . import store
from .paperclip import PaperclipError, board, config

log = logging.getLogger(__name__)

STANDUP_WAIT_MINUTES = 20  # hand off whatever has come in by then


def connected(conn: psycopg.Connection) -> bool:
    cfg = config(conn)
    return bool(cfg.get("board_token") and cfg.get("company_id") and cfg.get("eic_agent_id"))


def dashboard_url(story_id: int | None = None) -> str:
    base = os.environ.get("WASSUP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/?story={story_id}" if story_id else base


def _story_block(s: dict) -> str:
    return (f"- Story id: {s['id']}\n- Desk: {s['desk'] or 'none'}\n- {s['item_count']} articles from {s['source_count']} outlets "
            f"in {s['country_count']} countries; {s['velocity']:.0f} in the last hour; significance {s['significance']:.1f}\n"
            f"- Dashboard: {dashboard_url(s['id'])}")


def escalate(conn: psycopg.Connection) -> int:
    if not connected(conn):
        return 0
    cfg = load_yaml("newsroom.yaml")
    surge, eic = cfg.get("surge") or {}, cfg.get("editor_in_chief") or {}
    used = conn.execute("SELECT count(*) n FROM escalations WHERE created_at > now() - interval '24 hours'").fetchone()["n"]
    room = int(eic.get("max_escalations_per_day", 8)) - used
    if room <= 0:
        return 0
    flagged = """EXISTS (SELECT 1 FROM briefs b WHERE b.story_id = s.id AND b.created_at > now() - interval '6 hours'
                         AND b.meta->>'escalate' = 'true')"""
    candidates = conn.execute(
        f"""SELECT s.id, coalesce(s.title_en, s.title) title, s.desk, s.item_count, s.source_count, s.country_count,
                   s.velocity, s.significance, s.breaking, {flagged} AS flagged,
                   (SELECT string_agg(b.body, E'\\n\\n' ORDER BY b.created_at DESC) FROM briefs b
                     WHERE b.story_id = s.id AND b.kind = 'story') AS briefs
            FROM stories s
            WHERE s.routed AND s.last_seen > now() - interval '6 hours'
              AND NOT EXISTS (SELECT 1 FROM escalations e WHERE e.story_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM newsroom_agents a WHERE a.focus_story_id = s.id AND a.status = 'active')
              AND ((s.breaking AND s.velocity >= %(v)s AND s.significance >= %(sig)s) OR {flagged})
            ORDER BY s.significance DESC LIMIT %(room)s""",
        {"v": float(surge.get("min_velocity", 6)), "sig": float(surge.get("min_significance", 2.5)), "room": room}).fetchall()
    if not candidates:
        return 0
    pc, pcfg = board(conn), config(conn)
    done = 0
    for s in candidates:
        why = "a desk flagged it" if s["flagged"] and not s["breaking"] else "coverage is accelerating fast"
        desc = f"""Breaking story ({why}).

**{s['title']}**

{_story_block(s)}

{('Desk briefs so far:' + chr(10) + s['briefs'][:2500]) if s['briefs'] else 'No desk brief yet.'}

Decide whether this deserves a surge agent (a temporary reporter that checks it every few minutes until
it cools). If yes, request one through the Wassup API (see your instructions). Either way, note your
reasoning here and close this issue."""
        try:
            issue = pc.create_issue(pcfg["company_id"], f"Breaking: {s['title'][:150]}", desc, pcfg["eic_agent_id"],
                                    project_id=pcfg.get("project_id"), priority="high")
        except PaperclipError as e:
            log.warning("escalation failed for story %s: %s", s["id"], e)
            continue
        conn.execute("INSERT INTO escalations (story_id, paperclip_issue_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (s["id"], issue["id"]))
        store.event(conn, "eic", "escalation", f"Escalated to the Editor in Chief: {s['title']}", s["id"])
        conn.commit()
        done += 1
    return done


def call_standup(conn: psycopg.Connection, requested_by: str, topic: str | None = None) -> dict:
    """Open a standup and wake every desk so it reports on this heartbeat, not the next one."""
    open_ = conn.execute("SELECT id FROM standups WHERE status = 'collecting'").fetchone()
    if open_:
        return {"ok": True, "standup_id": open_["id"], "already_open": True}
    sid = conn.execute("INSERT INTO standups (requested_by, topic) VALUES (%s, %s) RETURNING id",
                       (requested_by, topic)).fetchone()["id"]
    store.event(conn, requested_by, "standup", f"Standup called{f' on {topic}' if topic else ''}")
    conn.commit()
    woke = 0
    if connected(conn):
        pc, pcfg = board(conn), config(conn)
        for a in store.active_agents(conn, "desk"):
            try:
                # A task wakes the desk now, and gives its report somewhere to live in Paperclip.
                # Paperclip treats a new issue with the same title and description as the same
                # issue, so each desk's task names the desk.
                pc.create_issue(pcfg["company_id"], f"Standup #{sid}: {a['name']} report",
                                f"{a['name']}: report what you are tracking, what you suspect connects to other desks, and what you need."
                                f"{f' Focus: {topic}' if topic else ''}", a["paperclip_agent_id"], project_id=pcfg.get("project_id"))
                woke += 1
            except PaperclipError as e:
                log.warning("could not wake %s for standup: %s", a["key"], e)
    return {"ok": True, "standup_id": sid, "desks_woken": woke}


def hand_off_standups(conn: psycopg.Connection) -> int:
    if not connected(conn):
        return 0
    n_desks = len(store.active_agents(conn, "desk"))
    ready = conn.execute(
        """SELECT s.*, count(r.agent_key) reports FROM standups s LEFT JOIN standup_reports r ON r.standup_id = s.id
           WHERE s.status = 'collecting' GROUP BY s.id
           HAVING count(r.agent_key) >= %s OR s.created_at < now() - %s * interval '1 minute'""",
        (n_desks, STANDUP_WAIT_MINUTES)).fetchall()
    pc, pcfg = board(conn), config(conn)
    for st in ready:
        reports = conn.execute(
            """SELECT r.body, a.name FROM standup_reports r JOIN newsroom_agents a ON a.key = r.agent_key
               WHERE r.standup_id = %s ORDER BY a.name""", (st["id"],)).fetchall()
        body = "\n\n".join(f"## {r['name']}\n{r['body']}" for r in reports) or "No desk reported in time."
        desc = f"""Standup{f' on: {st["topic"]}' if st['topic'] else ''}. {len(reports)} of {n_desks} desks reported.

{body}

Write up the standup: what each desk is tracking, the connections across desks that matter, and any
follow up you want. Save it with the Wassup API as a standup brief, assign follow up work to desks
as Paperclip issues if needed, then close this issue."""
        try:
            pc.create_issue(pcfg["company_id"], f"Standup #{st['id']}: write it up", desc[:60000], pcfg["eic_agent_id"],
                            project_id=pcfg.get("project_id"))
        except PaperclipError as e:
            log.warning("standup hand off failed: %s", e)
            continue
        conn.execute("UPDATE standups SET status = 'handed_off', handed_off_at = now() WHERE id = %s", (st["id"],))
        store.event(conn, "eic", "standup", f"Standup #{st['id']} handed to the Editor in Chief ({len(reports)} reports)")
        conn.commit()
    return len(ready)


def run_manager(conn: psycopg.Connection) -> int:
    """Pipeline step."""
    if not connected(conn):
        return 0
    return escalate(conn) + hand_off_standups(conn)
