"""Newsroom endpoints: the heartbeat Paperclip calls, the tools the Editor in Chief uses, and
what the dashboard shows."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .. import db
from . import store
from .desk import handle_heartbeat
from .manager import call_standup
from .paperclip import config, public_url
from .surge import SurgeRefused, request_surge, retire

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/newsroom")


def _check(conn, token: str | None) -> None:
    expected = config(conn).get("wassup_token")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(401, "missing or wrong x-wassup-token")


@router.post("/heartbeat")
def heartbeat(payload: dict, x_wassup_token: str | None = Header(None)) -> dict:
    """Called by Paperclip's HTTP adapter for desk and surge agents. Runs the agent's work
    before answering, so Paperclip's run covers the whole thing."""
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        try:
            return handle_heartbeat(conn, payload)
        except Exception as e:
            raise HTTPException(500, f"agent run failed: {e}") from e


@router.get("/status")
def status() -> dict:
    with db.connect() as conn:
        cfg = config(conn)
        counts = conn.execute(
            """SELECT count(*) FILTER (WHERE status = 'active') active, count(*) FILTER (WHERE kind = 'surge' AND status = 'active') surges
               FROM newsroom_agents""").fetchone()
    return {"connected": bool(cfg.get("board_token")), "set_up": bool(cfg.get("eic_agent_id")),
            "paperclip_url": public_url(), "company_id": cfg.get("company_id"), **counts}


@router.get("/agents")
def agents() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT a.key, a.kind, a.name, a.desk, a.focus_story_id, a.status, a.last_run_at, a.last_summary, a.created_at,
                      a.retired_at, a.paperclip_agent_id, coalesce(s.title_en, s.title) AS focus_title,
                      (SELECT count(*) FROM briefs b WHERE b.agent_key = a.key AND b.created_at > now() - interval '24 hours') briefs_24h,
                      (SELECT count(*) FROM follows f WHERE f.agent_key = a.key AND f.active) following
               FROM newsroom_agents a LEFT JOIN stories s ON s.id = a.focus_story_id
               WHERE a.status = 'active' OR a.retired_at > now() - interval '3 days'
               ORDER BY a.status, a.kind = 'surge' DESC, a.kind = 'eic' DESC, a.name""").fetchall()


@router.get("/briefs")
def briefs(hours: float = 24, kind: str | None = None, story_id: int | None = None, limit: int = Query(50, le=500)) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT b.id, b.kind, b.story_id, b.agent_key, a.name AS agent_name, b.title, b.body, b.meta, b.created_at
               FROM briefs b LEFT JOIN newsroom_agents a ON a.key = b.agent_key
               WHERE b.created_at > now() - %(h)s * interval '1 hour'
                 AND (%(kind)s::text IS NULL OR b.kind = %(kind)s) AND (%(sid)s::bigint IS NULL OR b.story_id = %(sid)s)
               ORDER BY b.created_at DESC LIMIT %(limit)s""",
            {"h": hours, "kind": kind, "sid": story_id, "limit": limit}).fetchall()


@router.get("/events")
def events(limit: int = Query(80, le=500)) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT e.id, e.agent_key, a.name AS agent_name, e.kind, e.text, e.story_id, e.created_at
               FROM newsroom_events e LEFT JOIN newsroom_agents a ON a.key = e.agent_key
               ORDER BY e.created_at DESC LIMIT %s""", (limit,)).fetchall()


@router.get("/search")
def search(q: str, cold: bool = True, days: int = 14, limit: int = Query(15, le=50)) -> list[dict]:
    with db.connect() as conn:
        return store.search_stories(conn, q, limit=limit, days=days, cold=cold)


@router.get("/overview")
def overview() -> dict:
    """One call that tells the Editor in Chief what the newsroom looks like right now."""
    with db.connect() as conn:
        desks = conn.execute(
            """SELECT a.desk, a.name, a.last_run_at, a.last_summary,
                      (SELECT count(*) FROM stories s WHERE s.routed AND s.desk = a.desk AND s.last_seen > now() - interval '24 hours') stories_24h
               FROM newsroom_agents a WHERE a.kind = 'desk' AND a.status = 'active' ORDER BY a.name""").fetchall()
        top = conn.execute(
            f"""SELECT {store.STORY_FIELDS} FROM stories s WHERE s.routed AND s.last_seen > now() - interval '24 hours'
                ORDER BY s.breaking DESC, s.significance DESC LIMIT 25""").fetchall()
        surges = conn.execute(
            """SELECT a.key, a.name, a.focus_story_id, a.created_at, a.last_summary FROM newsroom_agents a
               WHERE a.kind = 'surge' AND a.status = 'active'""").fetchall()
        recent = conn.execute(
            """SELECT b.kind, b.story_id, b.title, left(b.body, 600) AS body, a.name AS agent, b.created_at FROM briefs b
               LEFT JOIN newsroom_agents a ON a.key = b.agent_key WHERE b.created_at > now() - interval '24 hours'
               ORDER BY b.created_at DESC LIMIT 30""").fetchall()
        links = conn.execute(
            """SELECT l.a, l.b, l.kind, l.evidence->>'reason' AS reason, coalesce(sa.title_en, sa.title) AS a_title,
                      coalesce(sb.title_en, sb.title) AS b_title
               FROM story_links l JOIN stories sa ON sa.id = l.a JOIN stories sb ON sb.id = l.b
               WHERE l.created_by LIKE 'agent:%%' AND l.updated_at > now() - interval '24 hours'
               ORDER BY l.updated_at DESC LIMIT 30""").fetchall()
    return {"desks": desks, "top_stories": top, "surge_agents": surges, "recent_briefs": recent, "agent_links": links}


class BriefIn(BaseModel):
    kind: str
    body: str
    title: str | None = None
    story_id: int | None = None
    agent: str = "eic"


@router.post("/briefs")
def add_brief(b: BriefIn, x_wassup_token: str | None = Header(None)) -> dict:
    if b.kind not in ("daily", "standup", "story", "answer", "desk"):
        raise HTTPException(400, "kind must be daily, standup, story, answer or desk")
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        bid = store.add_brief(conn, b.kind, b.agent, b.body, story_id=b.story_id, title=b.title)
        store.event(conn, b.agent, "brief", f"{b.kind.title()} brief: {b.title or ''}".strip(), b.story_id)
        if b.kind == "standup":
            conn.execute("UPDATE standups SET status = 'done' WHERE status = 'handed_off'")
        conn.commit()
    return {"ok": True, "id": bid}


class LinkIn(BaseModel):
    a: int
    b: int
    relation: str
    reason: str
    agent: str = "eic"


@router.post("/links")
def add_link(l: LinkIn, x_wassup_token: str | None = Header(None)) -> dict:
    if l.relation not in store.AGENT_RELATIONS:
        raise HTTPException(400, f"relation must be one of {', '.join(store.AGENT_RELATIONS)}")
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        if conn.execute("SELECT count(*) n FROM stories WHERE id IN (%s, %s)", (l.a, l.b)).fetchone()["n"] != 2:
            raise HTTPException(404, "both stories must exist")
        store.add_link(conn, l.a, l.b, l.relation, l.reason, l.agent, weight=0.9)
        store.event(conn, l.agent, "link", f"Linked {l.a} {l.relation.replace('_', ' ')} {l.b}: {l.reason}", l.a)
        conn.commit()
    return {"ok": True}


class FollowIn(BaseModel):
    story_id: int
    reason: str | None = None
    agent: str = "eic"


@router.post("/follow")
def follow(f: FollowIn, x_wassup_token: str | None = Header(None)) -> dict:
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        store.follow(conn, f.story_id, f.agent, f.reason)
        # The story's desk follows it too, so it gets briefed on every check in.
        desk = conn.execute("SELECT desk FROM stories WHERE id = %s", (f.story_id,)).fetchone()
        if desk and desk["desk"]:
            store.follow(conn, f.story_id, f"desk:{desk['desk']}", f.reason)
        store.event(conn, f.agent, "follow", f"Following story {f.story_id}: {f.reason or ''}", f.story_id)
        conn.commit()
    return {"ok": True}


class SurgeIn(BaseModel):
    story_id: int
    reason: str
    agent: str = "eic"


@router.post("/surge")
def surge(s: SurgeIn, x_wassup_token: str | None = Header(None)) -> dict:
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        try:
            return request_surge(conn, s.story_id, s.reason, s.agent)
        except SurgeRefused as e:
            raise HTTPException(409, str(e)) from e


@router.post("/surge/{story_id}/retire")
def surge_retire(story_id: int, x_wassup_token: str | None = Header(None)) -> dict:
    with db.connect() as conn:
        _check(conn, x_wassup_token)
        retire(conn, f"surge:{story_id}", "Retired by the Editor in Chief.")
    return {"ok": True}


class StandupIn(BaseModel):
    topic: str | None = None
    agent: str = "eic"


@router.post("/standup")
def standup(s: StandupIn, x_wassup_token: str | None = Header(None)) -> dict:
    """Agents send their token. The dashboard (you, running Wassup locally) calls it without one."""
    with db.connect() as conn:
        if x_wassup_token:
            _check(conn, x_wassup_token)
        return call_standup(conn, s.agent if x_wassup_token else "you", s.topic)
