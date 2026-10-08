"""Handing investigations to the Investigator, a Paperclip agent on Claude Code
(investigator_instructions.md), hired by `wassup newsroom setup`.

Wassup opens a Paperclip issue for the Investigator when:
- an investigation's first summary is ready (the local model has read the first sources),
- you press ASK THE INVESTIGATOR (with a note, if you like),
- an active investigation has new material and the Investigator last looked more than
  `review_hours` ago.
At most `max_runs_per_day` hand-offs a day in all, so your Claude subscription is not used up.
"""
from __future__ import annotations

import logging
import os

import psycopg

from ..config import load_yaml
from ..newsroom import store
from ..newsroom.paperclip import PaperclipError, board, config

log = logging.getLogger(__name__)


def settings() -> dict:
    c = load_yaml("newsroom.yaml").get("investigator") or {}
    return {"review_hours": float(c.get("review_hours", 12)), "max_runs_per_day": int(c.get("max_runs_per_day", 6))}


def investigator_ready(conn: psycopg.Connection) -> bool:
    row = store.get_agent(conn, "investigator")
    pcfg = config(conn)
    return bool(row and row["status"] == "active" and row["paperclip_agent_id"] and pcfg.get("board_token") and pcfg.get("company_id"))


def hand_off(conn: psycopg.Connection) -> int:
    if not investigator_ready(conn):
        return 0
    c = settings()
    used = conn.execute("""SELECT count(*) AS n FROM newsroom_events WHERE agent_key = 'investigator' AND kind = 'investigation'
                           AND created_at > now() - interval '24 hours'""").fetchone()["n"]
    room = c["max_runs_per_day"] - used
    if room <= 0:
        return 0
    due = conn.execute(
        """SELECT v.* FROM investigations v
           WHERE v.status = 'active'
             AND (v.investigator_at IS NULL OR v.investigator_at < now() - interval '30 minutes')
             AND (v.ask_at IS NOT NULL
                  OR (v.investigator_at IS NULL AND v.summary IS NOT NULL)
                  OR (v.investigator_at < now() - %s * interval '1 hour'
                      AND EXISTS (SELECT 1 FROM investigation_sources s WHERE s.investigation_id = v.id
                                  AND s.status = 'analyzed' AND s.created_at > v.investigator_at)))
           ORDER BY v.ask_at DESC NULLS LAST, v.updated_at DESC LIMIT %s""", (c["review_hours"], room)).fetchall()
    if not due:
        return 0
    agent = store.get_agent(conn, "investigator")
    pc, pcfg = board(conn), config(conn)
    base = os.environ.get("WASSUP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    done = 0
    for v in due:
        why = ("The researcher asked you to look now" + (f": {v['ask_note']}" if v["ask_note"] else ".") if v["ask_at"]
               else "The first sources have been read and summarised." if v["investigator_at"] is None
               else "New material has come in since you last looked.")
        desc = f"""Investigation {v['id']}: **{v['title']}**

{why}

The researcher wants to know: {v['brief'] or '(see the title)'}

Start with `curl -s "$WASSUP_URL/api/investigations/{v['id']}/brief"`, then follow your instructions:
plan leads, follow the best ones, add what you find as sources, save your notes, and close this issue.

In the app: {base} (INVESTIGATE tab)."""
        try:
            issue = pc.create_issue(pcfg["company_id"], f"Investigate #{v['id']}: {v['title'][:140]}", desc,
                                    agent["paperclip_agent_id"], project_id=pcfg.get("project_id"),
                                    priority="high" if v["ask_at"] else "medium")
        except PaperclipError as e:
            log.warning("could not hand investigation %s to the Investigator: %s", v["id"], e)
            continue
        conn.execute("UPDATE investigations SET investigator_at = now(), investigator_issue = %s, ask_at = NULL WHERE id = %s",
                     (issue.get("id"), v["id"]))
        store.event(conn, "investigator", "investigation", f"Investigator assigned to: {v['title']}", None, investigation=v["id"])
        conn.commit()
        done += 1
    return done
