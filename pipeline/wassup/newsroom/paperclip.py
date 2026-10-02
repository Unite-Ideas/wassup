"""A small client for the Paperclip API.

Wassup talks to Paperclip two ways:
  * as the board (the operator), with a token you approve once in the browser, to create the
    company, hire and retire agents, and create issues;
  * as each agent, with that agent's own key, to post its reports on its issues.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx
import psycopg

from ..db import kv_get, kv_set

log = logging.getLogger(__name__)


class PaperclipError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Paperclip {status}: {message}")
        self.status = status


def api_url() -> str:
    return os.environ.get("PAPERCLIP_URL", "http://localhost:3100").rstrip("/") + "/api"


def public_url() -> str:
    """Where your browser reaches Paperclip (differs from api_url inside Docker)."""
    return os.environ.get("PAPERCLIP_PUBLIC_URL", os.environ.get("PAPERCLIP_URL", "http://localhost:3100")).rstrip("/")


@dataclass
class Paperclip:
    token: str | None = None
    base: str = ""

    def __post_init__(self):
        self.base = self.base or api_url()
        self.client = httpx.Client(timeout=60)

    def _req(self, method: str, path: str, json=None, run_id: str | None = None, params=None):
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        if run_id:
            headers["x-paperclip-run-id"] = run_id
        r = self.client.request(method, f"{self.base}{path}", json=json, headers=headers, params=params)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except Exception:
                msg = r.text
            raise PaperclipError(r.status_code, str(msg)[:300])
        return r.json() if r.content else None

    # Board connection -------------------------------------------------------------------

    def start_connect(self) -> dict:
        """Ask Paperclip for board access. Returns the pending token and the URL to approve it."""
        ch = self._req("POST", "/cli-auth/challenges", {"command": "wassup newsroom connect", "clientName": "Wassup", "requestedAccess": "board"})
        ch["browserUrl"] = public_url() + ch["approvalPath"]
        return ch

    def wait_for_approval(self, ch: dict, timeout_s: float = 600) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self._req("GET", f"/cli-auth/challenges/{ch['id']}", params={"token": ch["token"]})
            if st.get("status") == "approved":
                return True
            if st.get("status") in ("cancelled", "expired", "rejected"):
                return False
            time.sleep(2)
        return False

    # Thin wrappers over the endpoints Wassup uses ---------------------------------------

    def health(self) -> dict:
        return self._req("GET", "/health")

    def companies(self) -> list[dict]:
        return self._req("GET", "/companies")

    def create_company(self, name: str, description: str) -> dict:
        return self._req("POST", "/companies", {"name": name, "description": description})

    def projects(self, company_id: str) -> list[dict]:
        return self._req("GET", f"/companies/{company_id}/projects")

    def create_project(self, company_id: str, name: str, description: str = "") -> dict:
        return self._req("POST", f"/companies/{company_id}/projects", {"name": name, "description": description})

    def agents(self, company_id: str) -> list[dict]:
        return self._req("GET", f"/companies/{company_id}/agents")

    def create_agent(self, company_id: str, body: dict) -> dict:
        return self._req("POST", f"/companies/{company_id}/agents", body)

    def update_agent(self, agent_id: str, body: dict) -> dict:
        return self._req("PATCH", f"/agents/{agent_id}", body)

    def terminate_agent(self, agent_id: str) -> dict:
        return self._req("POST", f"/agents/{agent_id}/terminate")

    def create_agent_key(self, agent_id: str, name: str = "wassup") -> str:
        return self._req("POST", f"/agents/{agent_id}/keys", {"name": name})["token"]

    def invoke(self, agent_id: str) -> dict:
        return self._req("POST", f"/agents/{agent_id}/heartbeat/invoke", {})

    def create_issue(self, company_id: str, title: str, description: str, assignee: str | None,
                     status: str = "todo", project_id: str | None = None, priority: str = "medium") -> dict:
        body = {"title": title, "description": description, "status": status, "priority": priority}
        if assignee:
            body["assigneeAgentId"] = assignee
        if project_id:
            body["projectId"] = project_id
        return self._req("POST", f"/companies/{company_id}/issues", body)

    def update_issue(self, issue_id: str, run_id: str | None = None, **fields) -> dict:
        return self._req("PATCH", f"/issues/{issue_id}", fields, run_id=run_id)

    def comment(self, issue_id: str, body: str, run_id: str | None = None) -> dict:
        return self._req("POST", f"/issues/{issue_id}/comments", {"body": body}, run_id=run_id)

    def routines(self, company_id: str) -> list[dict]:
        return self._req("GET", f"/companies/{company_id}/routines")

    def create_routine(self, company_id: str, body: dict) -> dict:
        return self._req("POST", f"/companies/{company_id}/routines", body)

    def add_trigger(self, routine_id: str, body: dict) -> dict:
        return self._req("POST", f"/routines/{routine_id}/triggers", body)

    def update_routine(self, routine_id: str, body: dict) -> dict:
        return self._req("PATCH", f"/routines/{routine_id}", body)

    def run_routine(self, routine_id: str) -> dict:
        return self._req("POST", f"/routines/{routine_id}/run", {"source": "manual"})


def board(conn: psycopg.Connection) -> Paperclip:
    """Paperclip as the board, using the token approved by `wassup newsroom connect`."""
    cfg = kv_get(conn, "paperclip") or {}
    if not cfg.get("board_token"):
        raise RuntimeError("Wassup is not connected to Paperclip yet. Run: wassup newsroom connect")
    return Paperclip(token=cfg["board_token"])


def check_in_cron(minutes: int, offset: int) -> str:
    """Cron for "every N minutes", staggered so desks do not all hit the GPU at once."""
    if minutes >= 60:
        hours = max(1, minutes // 60)
        return f"{offset % 60} */{hours} * * *" if hours > 1 else f"{offset % 60} * * * *"
    return f"{offset % minutes}-59/{minutes} * * * *"


def ensure_check_in(pc: "Paperclip", company_id: str, project_id: str, agent_id: str, title: str,
                    minutes: int, offset: int, timezone: str, existing: dict | None = None) -> str:
    """A routine that hands the agent a check in task on a schedule. Each run has its own task,
    which the agent closes with its report."""
    if existing:
        return existing["id"]
    r = pc.create_routine(company_id, {"title": title, "description": "Review what moved, write briefs, report here, and close this task.",
                                       "assigneeAgentId": agent_id, "projectId": project_id, "priority": "low",
                                       "concurrencyPolicy": "skip_if_active", "catchUpPolicy": "skip_missed"})
    pc.add_trigger(r["id"], {"kind": "schedule", "cronExpression": check_in_cron(minutes, offset), "timezone": timezone})
    return r["id"]


def config(conn: psycopg.Connection) -> dict:
    return kv_get(conn, "paperclip") or {}


def save_config(conn: psycopg.Connection, **values) -> dict:
    cfg = config(conn)
    cfg.update(values)
    kv_set(conn, "paperclip", cfg)
    conn.commit()
    return cfg
