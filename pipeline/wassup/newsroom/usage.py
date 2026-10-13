"""How much Claude the newsroom uses, for the CLAUDE USAGE panel on the NEWSROOM tab.

Wassup's Claude agents (the Editor in Chief, the Investigator, the Standards Editor) run in
Paperclip, which records every run's tokens. Every 10 minutes this asks Paperclip for the
tokens per agent in each hour (a week back the first time) and keeps them, so hours and days
can be added up and charted. It also keeps the subscription's own usage meters (how much of
the 5 hour session and of the week is used), which is what actually limits a Claude
subscription; tokens explain where that went.

On a subscription Paperclip records the dollar cost as 0 (included in the plan).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg

from ..db import kv_get, kv_set
from .paperclip import board, config

log = logging.getLogger(__name__)

BACKFILL = timedelta(days=7)
MAX_HOURS_PER_RUN = 200


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def run_usage(conn: psycopg.Connection, pc=None, now: datetime | None = None) -> int:
    cfg = config(conn)
    if not (cfg.get("board_token") and cfg.get("company_id")):
        return 0
    pc = pc or board(conn)
    company = cfg["company_id"]
    now = now or datetime.now(timezone.utc)
    current = _hour(now)
    done = kv_get(conn, "claude_usage.done_until")
    start = datetime.fromisoformat(done) if done else current - BACKFILL
    start = max(start, current - BACKFILL)
    hours, h = [], start
    while h <= current and len(hours) < MAX_HOURS_PER_RUN:
        hours.append(h)
        h += timedelta(hours=1)
    stored = 0
    for h in hours:
        end = h + timedelta(hours=1) - timedelta(milliseconds=1)
        rows = pc.costs_by_agent(company, h.isoformat(), end.isoformat())
        rows = [r for r in rows if (r.get("inputTokens") or 0) + (r.get("cachedInputTokens") or 0) + (r.get("outputTokens") or 0) > 0
                or (r.get("costCents") or 0) > 0]
        conn.execute("DELETE FROM claude_usage WHERE hour = %s", (h,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO claude_usage (hour, agent_id, agent_name, input_tokens, cached_tokens, output_tokens, cost_cents, runs)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [(h, r["agentId"], r.get("agentName"), int(r.get("inputTokens") or 0), int(r.get("cachedInputTokens") or 0),
                  int(r.get("outputTokens") or 0), int(r.get("costCents") or 0),
                  int(r.get("apiRunCount") or 0) + int(r.get("subscriptionRunCount") or 0)) for r in rows])
        stored += len(rows)
    if hours:
        # The last two hours are asked for again next time: a run can finish after the hour it started in.
        kv_set(conn, "claude_usage.done_until", max(start, hours[-1] - timedelta(hours=1)).isoformat())
    conn.commit()
    _meters(conn, pc, company, now)
    return stored


def _meters(conn, pc, company: str, now: datetime) -> None:
    try:
        results = pc.quota_windows(company)
    except Exception as e:  # noqa: BLE001
        kv_set(conn, "claude_usage.meters", {"ok": False, "error": str(e)[:300], "at": now.isoformat()})
        conn.commit()
        return
    claude = next((r for r in results if r.get("provider") == "anthropic"), None)
    if not claude or not claude.get("ok"):
        kv_set(conn, "claude_usage.meters", {"ok": False, "error": (claude or {}).get("error") or "Paperclip reported no Claude meters",
                                             "at": now.isoformat()})
        conn.commit()
        return
    at = now.replace(second=0, microsecond=0)
    windows = claude.get("windows") or []
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO claude_quota (at, label, used_percent, resets_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (at, label) DO UPDATE SET used_percent = EXCLUDED.used_percent, resets_at = EXCLUDED.resets_at""",
            [(at, w["label"], w.get("usedPercent"), w.get("resetsAt")) for w in windows if w.get("label")])
    conn.execute("DELETE FROM claude_quota WHERE at < now() - interval '30 days'")
    kv_set(conn, "claude_usage.meters", {"ok": True, "at": now.isoformat(), "source": claude.get("source"),
                                         "windows": [{k: w.get(k) for k in ("label", "usedPercent", "resetsAt", "valueLabel")} for w in windows]})
    conn.commit()


def summary(conn, hours: int = 48, days: int = 30) -> dict:
    """Hourly and daily token totals, totals per agent, and the latest subscription meters."""
    tot = "sum(input_tokens + cached_tokens + output_tokens)::bigint"
    hourly = conn.execute(
        f"""SELECT hour, {tot} AS tokens, sum(input_tokens)::bigint AS input, sum(cached_tokens)::bigint AS cached, sum(output_tokens)::bigint AS output,
                   sum(runs)::bigint AS runs FROM claude_usage WHERE hour > now() - make_interval(hours => %s) GROUP BY hour ORDER BY hour""",
        (hours,)).fetchall()
    daily = conn.execute(
        f"""SELECT date_trunc('day', hour AT TIME ZONE %s) AS day, {tot} AS tokens, sum(input_tokens)::bigint AS input,
                   sum(cached_tokens)::bigint AS cached, sum(output_tokens)::bigint AS output, sum(runs)::bigint AS runs
            FROM claude_usage WHERE hour > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1""",
        (_tz(), days)).fetchall()
    agents = conn.execute(
        f"""SELECT agent_name,
                   coalesce(sum(input_tokens + cached_tokens + output_tokens) FILTER (WHERE hour > now() - interval '24 hours'), 0)::bigint AS day,
                   coalesce(sum(input_tokens + cached_tokens + output_tokens) FILTER (WHERE hour > now() - interval '7 days'), 0)::bigint AS week,
                   {tot} AS month,
                   coalesce(sum(output_tokens) FILTER (WHERE hour > now() - interval '24 hours'), 0)::bigint AS day_output,
                   coalesce(sum(runs) FILTER (WHERE hour > now() - interval '24 hours'), 0)::bigint AS day_runs
            FROM claude_usage WHERE hour > now() - interval '30 days' GROUP BY agent_name ORDER BY day DESC, month DESC""").fetchall()
    trend = conn.execute(
        """SELECT at, label, used_percent FROM claude_quota WHERE at > now() - interval '48 hours' ORDER BY at""").fetchall()
    return {"hourly": [{**r, "hour": r["hour"].isoformat()} for r in hourly],
            "daily": [{**r, "day": r["day"].date().isoformat()} for r in daily],
            "agents": agents, "meters": kv_get(conn, "claude_usage.meters"),
            "meter_trend": [{**r, "at": r["at"].isoformat()} for r in trend],
            "connected": bool(config(conn).get("company_id"))}


def _tz() -> str:
    import os

    return os.environ.get("TZ") or "UTC"
