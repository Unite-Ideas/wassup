"""Surge agents: temporary agents hired to track one fast moving story, retired when it cools.

Wassup owns their lifecycle, so the caps in config/newsroom.yaml hold no matter who asks:
the Editor in Chief requests one through the Wassup API, Wassup checks the limits, hires it in
Paperclip, and retires it once the story goes quiet."""
from __future__ import annotations

import logging
import os

import psycopg

from ..config import load_yaml
from . import store
from .paperclip import PaperclipError, board, config, ensure_check_in

log = logging.getLogger(__name__)


class SurgeRefused(Exception):
    pass


def agent_url() -> str:
    """Where Paperclip reaches Wassup's heartbeat endpoint (the app service in Docker)."""
    return os.environ.get("WASSUP_AGENT_URL", "http://app:8000/api/newsroom/heartbeat")


def http_adapter(conn: psycopg.Connection) -> dict:
    return {"url": agent_url(), "method": "POST", "headers": {"x-wassup-token": config(conn).get("wassup_token", "")}}


def request_surge(conn: psycopg.Connection, story_id: int, reason: str, requested_by: str) -> dict:
    cfg = load_yaml("newsroom.yaml")
    limits, surge = cfg.get("limits") or {}, cfg.get("surge") or {}
    key = f"surge:{story_id}"
    existing = store.get_agent(conn, key)
    if existing and existing["status"] == "active":
        return {"ok": True, "already": True, "agent": key}
    story = conn.execute("SELECT id, coalesce(title_en, title) title, desk, routed FROM stories WHERE id = %s", (story_id,)).fetchone()
    if not story:
        raise SurgeRefused(f"story {story_id} not found")
    if len(store.active_agents(conn, "surge")) >= int(surge.get("max_active", 5)):
        raise SurgeRefused(f"already at the limit of {surge.get('max_active', 5)} surge agents; retire one first")
    if len(store.active_agents(conn)) >= int(limits.get("max_agents", 20)):
        raise SurgeRefused(f"the newsroom is at its limit of {limits.get('max_agents', 20)} agents")

    pc_cfg = config(conn)
    pc = board(conn)
    short = story["title"] if len(story["title"]) <= 60 else story["title"][:57] + "..."
    agent = pc.create_agent(pc_cfg["company_id"], {
        "name": f"Surge: {short}", "role": "researcher", "title": "Surge reporter",
        "reportsTo": pc_cfg.get("eic_agent_id"),
        "capabilities": f"Tracks one breaking story closely until it cools: {story['title']}",
        "adapterType": "http", "adapterConfig": http_adapter(conn),
        "runtimeConfig": {"heartbeat": {"enabled": False, "wakeOnDemand": True}},
    })
    api_key = pc.create_agent_key(agent["id"])
    # The case file: why the surge exists and, at the end, why it retired.
    case = pc.create_issue(pc_cfg["company_id"], f"Surge: {short}",
                           f"Why: {reason}\n\nRequested by {requested_by}. Each check in is its own task under this agent.",
                           assignee=None, status="backlog", project_id=pc_cfg.get("project_id"), priority="high")
    routine_id = ensure_check_in(pc, pc_cfg["company_id"], pc_cfg.get("project_id"), agent["id"], f"Check in: {agent['name']}",
                                 int(surge.get("heartbeat_minutes", 10)), story_id % 10,
                                 (load_yaml("newsroom.yaml").get("company") or {}).get("timezone", "UTC"))
    store.upsert_agent(conn, key, "surge", agent["name"], desk=story["desk"], focus_story_id=story_id,
                       paperclip_agent_id=agent["id"], paperclip_api_key=api_key, log_issue_id=case["id"],
                       status="active", meta={"reason": reason, "requested_by": requested_by, "routine_id": routine_id})
    store.follow(conn, story_id, key, reason)
    store.event(conn, key, "surge_hired", f"Surge agent hired for: {story['title']} ({reason})", story_id)
    conn.commit()
    try:
        pc.run_routine(routine_id)  # first check in right away
    except PaperclipError as e:
        log.warning("could not start surge agent %s: %s", key, e)
    return {"ok": True, "agent": key, "paperclip_agent_id": agent["id"]}


def retire(conn: psycopg.Connection, key: str, reason: str) -> None:
    agent = store.get_agent(conn, key)
    if not agent or agent["status"] != "active":
        return
    try:
        pc = board(conn)
        if (agent["meta"] or {}).get("routine_id"):
            pc.update_routine(agent["meta"]["routine_id"], {"status": "archived"})
        pc.terminate_agent(agent["paperclip_agent_id"])
        if agent["log_issue_id"]:
            pc.update_issue(agent["log_issue_id"], status="done", comment=f"Retired. {reason}")
    except (PaperclipError, RuntimeError) as e:
        log.warning("could not retire %s in Paperclip: %s", key, e)
    conn.execute("UPDATE newsroom_agents SET status = 'retired', retired_at = now() WHERE key = %s", (key,))
    if agent["focus_story_id"]:
        store.unfollow(conn, agent["focus_story_id"], key)
    store.event(conn, key, "surge_retired", f"{agent['name']} retired. {reason}", agent["focus_story_id"])
    conn.commit()
