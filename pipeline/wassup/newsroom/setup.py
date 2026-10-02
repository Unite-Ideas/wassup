"""`wassup newsroom connect` and `wassup newsroom setup`.

connect: asks Paperclip for board access; you approve it once in the browser.
setup:   creates (or updates) the Wassup company in Paperclip: the Editor in Chief, one agent per
         desk, their keys and log issues, and the daily brief and standup routines. Safe to run
         again after changing config/newsroom.yaml or config/desks.yaml.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import psycopg

from ..config import load_yaml
from . import store
from .paperclip import Paperclip, PaperclipError, board, config, ensure_check_in, public_url, save_config
from .surge import http_adapter

log = logging.getLogger(__name__)

EIC_INSTRUCTIONS = Path(__file__).with_name("eic_instructions.md")


def connect(conn: psycopg.Connection, out=print) -> bool:
    pc = Paperclip()
    try:
        pc.health()
    except Exception as e:
        out(f"Cannot reach Paperclip at {pc.base}: {e}")
        return False
    ch = pc.start_connect()
    out("\nOpen this link in your browser (sign in to Paperclip first if asked) and click Approve:\n")
    out(f"    {ch['browserUrl']}\n")
    out("Waiting for approval (up to 10 minutes)...")
    if not pc.wait_for_approval(ch):
        out("Not approved. Run `wassup newsroom connect` again to retry.")
        return False
    save_config(conn, board_token=ch["boardApiToken"])
    out("Connected. Next: wassup newsroom setup")
    return True


def _eic_body(conn, cfg: dict, company_id: str) -> dict:
    eic = cfg.get("editor_in_chief") or {}
    wassup_url = os.environ.get("WASSUP_INTERNAL_URL", "http://app:8000")
    return {
        "name": eic.get("name", "Editor in Chief"), "role": "ceo", "title": "Editor in Chief",
        "capabilities": "Runs the Wassup newsroom: daily brief, standups, cross desk connections, surge decisions.",
        "adapterType": "claude_local",
        "adapterConfig": {
            "model": eic.get("model", "claude-sonnet-5-5"),
            "cwd": "/paperclip/workspaces/wassup-editor-in-chief",
            "timeoutSec": 1800,
            "maxTurnsPerRun": 80,
            "env": {"WASSUP_URL": wassup_url, "WASSUP_TOKEN": config(conn)["wassup_token"]},
        },
        "runtimeConfig": {"heartbeat": {"enabled": False, "wakeOnDemand": True}},
        "budgetMonthlyCents": int(float(eic.get("budget_monthly_usd", 0)) * 100),
    }


def _desk_body(conn, desk: dict, eic_id: str, minutes: int) -> dict:
    return {
        "name": f"{desk['name']} Desk", "role": "researcher", "title": f"{desk['name']} desk",
        "reportsTo": eic_id,
        "capabilities": desk.get("description") or f"Reviews and briefs the {desk['name']} desk every {minutes} minutes on a local model.",
        "adapterType": "http", "adapterConfig": http_adapter(conn),
        # Desks run on a check in routine (see setup), not a bare timer, so every check in is a
        # Paperclip task the desk closes with its report.
        "runtimeConfig": {"heartbeat": {"enabled": False, "wakeOnDemand": True}},
    }


def setup(conn: psycopg.Connection, out=print) -> dict:
    cfg = load_yaml("newsroom.yaml")
    pcfg = config(conn)
    if not pcfg.get("wassup_token"):
        pcfg = save_config(conn, wassup_token=secrets.token_urlsafe(32))
    pc = board(conn)
    company = cfg.get("company") or {}

    company_id = pcfg.get("company_id")
    if company_id and not any(c["id"] == company_id for c in pc.companies()):
        company_id = None
    if not company_id:
        existing = [c for c in pc.companies() if c["name"] == company.get("name", "Wassup") and c.get("status") != "archived"]
        company_id = existing[0]["id"] if existing else pc.create_company(company.get("name", "Wassup"), company.get("description", ""))["id"]
        out(f"Company: {company.get('name', 'Wassup')} ({company_id})")
    projects = [p for p in pc.projects(company_id) if p["name"] == "Newsroom"]
    project_id = projects[0]["id"] if projects else pc.create_project(company_id, "Newsroom", "Wassup newsroom work")["id"]
    pcfg = save_config(conn, company_id=company_id, project_id=project_id)
    live = {a["id"]: a for a in pc.agents(company_id) if a.get("status") != "terminated"}

    def ensure(key: str, kind: str, body: dict, desk: str | None = None) -> str:
        row = store.get_agent(conn, key)
        if row and row["paperclip_agent_id"] in live:
            agent_id = row["paperclip_agent_id"]
            update = {k: v for k, v in body.items() if k in ("adapterConfig", "runtimeConfig", "capabilities", "title", "reportsTo", "budgetMonthlyCents")}
            pc.update_agent(agent_id, update)
            api_key = row["paperclip_api_key"]
            out(f"  updated {body['name']}")
        else:
            if kind == "eic":
                body = {**body, "instructionsBundle": {"entryFile": "AGENTS.md", "files": {"AGENTS.md": EIC_INSTRUCTIONS.read_text(encoding="utf-8")}}}
            agent_id = pc.create_agent(company_id, body)["id"]
            api_key = pc.create_agent_key(agent_id)
            out(f"  hired {body['name']}")
        store.upsert_agent(conn, key, kind, body["name"], desk=desk, paperclip_agent_id=agent_id, paperclip_api_key=api_key,
                           status="active")
        conn.commit()
        return agent_id

    out("Agents:")
    eic_id = ensure("eic", "eic", _eic_body(conn, cfg, company_id))
    save_config(conn, eic_agent_id=eic_id)
    minutes = int((cfg.get("desks") or {}).get("heartbeat_minutes", 60))
    tz = company.get("timezone", "UTC")
    desks = load_yaml("desks.yaml").get("desks", [])
    routines = {r["title"]: r for r in pc.routines(company_id)}
    for i, d in enumerate(desks):
        agent_id = ensure(f"desk:{d['key']}", "desk", _desk_body(conn, d, eic_id, minutes), desk=d["key"])
        title = f"Check in: {d['name']}"
        # Stagger desks across the hour so they take turns on the GPU.
        ensure_check_in(pc, company_id, project_id, agent_id, title, minutes, 3 + i * max(1, min(minutes, 60) // max(1, len(desks))),
                        tz, routines.get(title))
    # Desks removed from desks.yaml stop running.
    wanted = {f"desk:{d['key']}" for d in desks}
    for a in store.active_agents(conn, "desk"):
        if a["key"] not in wanted:
            try:
                pc.terminate_agent(a["paperclip_agent_id"])
            except PaperclipError:
                pass
            conn.execute("UPDATE newsroom_agents SET status = 'retired', retired_at = now() WHERE key = %s", (a["key"],))
            out(f"  retired {a['name']}")
    conn.commit()

    out("Routines:")
    eic = cfg.get("editor_in_chief") or {}
    have = {r["title"]: r for r in pc.routines(company_id)}
    for title, desc, cron in [
        ("Daily brief", "Write today's daily brief from the overview and the last day of desk briefs. Save it as a daily brief, then close this issue.",
         eic.get("daily_brief_cron", "45 6 * * *")),
        ("Afternoon standup", "Call the afternoon standup through the Wassup API, then close this issue. You will get a second issue with the desk reports to write up.",
         eic.get("standup_cron", "50 16 * * *")),
    ]:
        if title in have:
            out(f"  {title} exists")
            continue
        r = pc.create_routine(company_id, {"title": title, "description": desc, "assigneeAgentId": eic_id, "projectId": project_id,
                                           "concurrencyPolicy": "skip_if_active", "catchUpPolicy": "skip_missed"})
        pc.add_trigger(r["id"], {"kind": "schedule", "cronExpression": cron, "timezone": tz})
        out(f"  {title}: {cron} ({tz})")

    out(f"\nDone. The newsroom is live: {public_url()}")
    return {"company_id": company_id, "eic_agent_id": eic_id, "desks": len(desks)}
