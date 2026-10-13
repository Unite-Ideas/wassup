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


def run_usage(conn: psycopg.Connection, pc=None, now: datetime | None = None, meters_client=None) -> int:
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
    _meters(conn, pc, company, now, meters_client)
    return stored


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Anthropic limits how often the meters may be read; when it says to slow down, wait this long,
# doubling each time it says so again.
BACKOFF_FIRST = timedelta(minutes=30)
BACKOFF_MAX = timedelta(hours=6)
WINDOWS = [("five_hour", "Current session"), ("seven_day", "Current week (all models)"),
           ("seven_day_sonnet", "Current week (Sonnet only)"), ("seven_day_opus", "Current week (Opus only)")]


def _percent(u) -> float | None:
    if u is None:
        return None
    return min(100.0, round(u * 100 if u < 1 else u))


def parse_meters(body: dict) -> list[dict]:
    """Anthropic's usage reply as meters: the 5 hour session and the weekly limits."""
    out = [{"label": label, "usedPercent": _percent((body.get(key) or {}).get("utilization")),
            "resetsAt": (body.get(key) or {}).get("resets_at"), "valueLabel": None}
           for key, label in WINDOWS if body.get(key) is not None]
    extra = body.get("extra_usage")
    if extra and extra.get("is_enabled") is not False:
        out.append({"label": "Extra usage", "usedPercent": _percent(extra.get("utilization")), "resetsAt": None, "valueLabel": None})
    return out


def _direct(conn, now: datetime, client=None) -> tuple[list[dict] | None, str | None]:
    """Read the meters from Anthropic with the newsroom's Claude login (CLAUDE_CODE_OAUTH_TOKEN).
    Returns (meters, None), or (None, why not)."""
    import os

    import httpx

    token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    wait = kv_get(conn, "claude_usage.backoff") or {}
    if wait.get("until") and now < datetime.fromisoformat(wait["until"]):
        return None, wait.get("why") or "waiting"
    client = client or httpx.Client(timeout=15)
    r = client.get(USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    if r.status_code == 200:
        kv_set(conn, "claude_usage.backoff", {})
        return parse_meters(r.json()), None
    if r.status_code == 429:
        last = timedelta(seconds=wait.get("seconds") or 0)
        delay = min(BACKOFF_MAX, max(BACKOFF_FIRST, last * 2))
        try:
            delay = max(delay, timedelta(seconds=int(r.headers.get("retry-after") or 0)))
        except ValueError:
            pass
        why = "Anthropic asked Wassup to read the meters less often"
    else:
        delay = BACKOFF_MAX
        try:
            why = f"Anthropic said {r.status_code}: {r.json().get('error', {}).get('message', '')}"[:200]
        except Exception:  # noqa: BLE001
            why = f"Anthropic said {r.status_code}"
    kv_set(conn, "claude_usage.backoff", {"until": (now + delay).isoformat(), "seconds": int(delay.total_seconds()), "why": why})
    return None, why


def _meters(conn, pc, company: str, now: datetime, client=None) -> None:
    import os

    windows, why = None, None
    if (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        # The login is a setup-token, which Paperclip can only read the meters with by opening
        # Claude's /usage screen, and that does not work in its container. Ask Anthropic directly.
        try:
            windows, why = _direct(conn, now, client)
        except Exception as e:  # noqa: BLE001
            why = f"could not reach Anthropic: {e}"[:200]
    else:
        try:
            claude = next((r for r in pc.quota_windows(company) if r.get("provider") == "anthropic"), None)
            if claude and claude.get("ok"):
                windows = claude.get("windows") or []
            else:
                why = (claude or {}).get("error") or "Paperclip reported no Claude meters"
        except Exception as e:  # noqa: BLE001
            why = str(e)[:300]
    if windows is None:
        prev = kv_get(conn, "claude_usage.meters") or {}
        if prev.get("ok"):
            # Keep showing the last good reading, with its time and why it is not newer.
            kv_set(conn, "claude_usage.meters", {**prev, "note": why})
        else:
            kv_set(conn, "claude_usage.meters", {"ok": False, "error": why, "at": now.isoformat()})
        conn.commit()
        return
    at = now.replace(second=0, microsecond=0)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO claude_quota (at, label, used_percent, resets_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (at, label) DO UPDATE SET used_percent = EXCLUDED.used_percent, resets_at = EXCLUDED.resets_at""",
            [(at, w["label"], w.get("usedPercent"), w.get("resetsAt")) for w in windows if w.get("label")])
    conn.execute("DELETE FROM claude_quota WHERE at < now() - interval '30 days'")
    kv_set(conn, "claude_usage.meters", {"ok": True, "at": now.isoformat(),
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
