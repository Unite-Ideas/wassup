"""Desk and surge agents. Paperclip wakes them (heartbeat) through its HTTP adapter; the work
runs here, on the local model, and the agent reports back on its Paperclip issue.

A desk heartbeat:
  1. reviews the stories on its desk that moved since its last run,
  2. picks what matters, what to keep following, and what was misrouted,
  3. writes briefs on the stories that matter,
  4. looks for connections to stories on other desks and explains them,
  5. answers any open standup, and any assignment the Editor in Chief gave it.

A surge agent does the same for one fast moving story, every few minutes, and retires once
the story goes quiet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..config import load_yaml
from ..locate import unlock
from . import store
from .llm import BOOL, INTS, LLM, NUM, STR, STRS, default_llm, obj
from .paperclip import Paperclip, PaperclipError

log = logging.getLogger(__name__)

NEWSROOM_BRIEF = """You are the {desk_name} desk at Wassup, a news intelligence service for one analyst.
The analyst cares about wars, invasions, mass migration, US politics and Congress, the UN, and
government document releases (Epstein, JFK, UAP, FOIA). Not sports or celebrity news unless it is
part of a larger story. Be factual and concise. State media is propaganda: report what it claims,
but trust independent sources over it. Never invent facts that are not in the material."""


def _cfg() -> dict:
    return load_yaml("newsroom.yaml")


def _desk_name(desk: str | None) -> str:
    for d in load_yaml("desks.yaml").get("desks", []):
        if d["key"] == desk:
            return d["name"]
    return desk or "general"


def _story_line(i: int, s: dict) -> str:
    extra = []
    if s.get("followed"):
        extra.append("FOLLOWING")
    if s.get("breaking"):
        extra.append(f"BREAKING {s['velocity']:.0f}/hr")
    if s.get("new_items") is not None:
        extra.append(f"{s['new_items']} new")
    where = f" | on the map at {s['place']}{', ' + s['place_country'] if s.get('place_country') and s['place_country'] not in s['place'] else ''}" if s.get("place") else ""
    return (f"[{i}] {s['title']} | {s['item_count']} articles, {s['source_count']} outlets, "
            f"{s['country_count']} countries, significance {s['significance']:.1f}{where}"
            + (f" | {', '.join(extra)}" if extra else ""))


# --- the steps ---------------------------------------------------------------------------

def review_queue(llm: LLM, desk_name: str, queue: list[dict]) -> dict:
    listing = "\n".join(_story_line(i, s) for i, s in enumerate(queue))
    prompt = f"""{NEWSROOM_BRIEF.format(desk_name=desk_name)}

These stories on your desk moved since your last check:
{listing}

Decide:
- summary: two to four sentences on what is happening on your desk right now.
- important: indexes of the stories that genuinely matter (at most 5).
- follow: indexes worth tracking over the coming days (at most 3). Keep following stories marked FOLLOWING if they are still developing.
- unfollow: indexes marked FOLLOWING that are finished or no longer matter.
- misrouted: indexes that do not belong on this desk or are not news the analyst wants (sports, celebrity, ads, trivia).
- misplaced: indexes whose map location is clearly wrong for the story (for example a Canadian story placed in New Zealand)."""
    out = llm(prompt, obj(summary=STR, important=INTS, follow=INTS, unfollow=INTS, misrouted=INTS, misplaced=INTS))
    n = len(queue)
    for k in ("important", "follow", "unfollow", "misrouted", "misplaced"):
        out[k] = [i for i in dict.fromkeys(out.get(k) or []) if isinstance(i, int) and 0 <= i < n]
    return out


def write_brief(llm: LLM, desk_name: str, digest: dict) -> dict:
    prompt = f"""{NEWSROOM_BRIEF.format(desk_name=desk_name)}

Write a brief on this story from the coverage below. If there is a previous brief, update it: say
what changed.

{store.digest_text(digest)}

Return:
- brief: three to six sentences. What happened, who is involved, why it matters.
- key_points: up to four short factual points.
- watch_for: one sentence on what to watch next.
- escalate: true only if this is a major development the Editor in Chief must see now.
- confidence: 0 to 1, how well the coverage supports the brief."""
    return llm(prompt, obj(brief=STR, key_points=STRS, watch_for=STR, escalate=BOOL, confidence=NUM))


def find_links(llm: LLM, desk_name: str, story: dict, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    listing = "\n".join(f"[{i}] {c['title']} (desk: {_desk_name(c['desk'])})" for i, c in enumerate(candidates))
    prompt = f"""{NEWSROOM_BRIEF.format(desk_name=desk_name)}

Your story: {story['title']}
Brief: {story.get('brief_text', '')}

Stories on other desks:
{listing}

Which of these are genuinely connected to your story, in a way the analyst would want drawn on the
evidence board? Only real connections: one causing or responding to another, an escalation, one
being part of the other, contradicting accounts, or a clear parallel. At most 3. For each give the
candidate index, the relation, and one sentence explaining the connection. Return an empty list if
none are connected."""
    schema = obj(links={"type": "array", "items": obj(
        candidate={"type": "integer"}, relation={"type": "string", "enum": store.AGENT_RELATIONS}, reason=STR)})
    out = llm(prompt, schema).get("links") or []
    return [l for l in out if isinstance(l.get("candidate"), int) and 0 <= l["candidate"] < len(candidates)][:3]


def answer_directive(llm: LLM, desk_name: str, question: str, stories: list[dict]) -> dict:
    listing = "\n\n".join(store.digest_text(s) for s in stories) or "(no matching coverage found)"
    prompt = f"""{NEWSROOM_BRIEF.format(desk_name=desk_name)}

The Editor in Chief asked your desk:
{question}

Relevant coverage Wassup has collected (including stories in cold storage):
{listing}

Answer from this coverage only. Say plainly what is not known.
Return:
- answer: the answer, in a few short paragraphs of markdown.
- story_ids: the ids of the stories your answer relies on.
- follow_ids: ids of stories your desk should keep tracking because of this."""
    return llm(prompt, obj(answer=STR, story_ids=INTS, follow_ids=INTS))


def standup_report(llm: LLM, desk_name: str, summary: str, briefs: list[dict], topic: str | None) -> str:
    listing = "\n".join(f"- {b['title']}: {b['body'][:400]}" for b in briefs) or "- (no new briefs)"
    prompt = f"""{NEWSROOM_BRIEF.format(desk_name=desk_name)}

Standup. The Editor in Chief wants each desk's report{f' on: {topic}' if topic else ''}.
Your current read of the desk: {summary}
Your recent briefs:
{listing}

Return report: a short markdown report with three parts: what you are tracking, what you suspect
connects to other desks, and what you need from the Editor in Chief."""
    return llm(prompt, obj(report=STR))["report"]


# --- runs ---------------------------------------------------------------------------------

def run_desk(conn: psycopg.Connection, agent: dict, llm: LLM) -> dict:
    cfg = _cfg().get("desks") or {}
    desk, key = agent["desk"], agent["key"]
    name = _desk_name(desk)
    since = agent["last_run_at"] or datetime.now(timezone.utc) - timedelta(hours=3)
    queue = store.desk_queue(conn, desk, key, since, int(cfg.get("max_stories_per_run", 20)))
    report = {"desk": desk, "stories_reviewed": len(queue), "briefs": [], "links": [], "followed": [], "misrouted": []}
    if not queue:
        report["summary"] = "Quiet since the last check: nothing new on this desk."
        return report

    review = review_queue(llm, name, queue)
    report["summary"] = review["summary"]
    for i in review["follow"]:
        store.follow(conn, queue[i]["id"], key, "desk review")
        report["followed"].append(queue[i]["title"])
    for i in review["unfollow"]:
        store.unfollow(conn, queue[i]["id"], key)
    for i in review["misrouted"]:
        s = queue[i]
        if i in review["important"] or s["significance"] >= 3.5:
            continue  # the desk is unsure; a big story stays put
        conn.execute("UPDATE stories SET routed = false, excluded_reason = 'desk_dismissed' WHERE id = %s", (s["id"],))
        report["misrouted"].append(s["title"])
    for i in review["misplaced"]:
        unlock(conn, queue[i]["id"])  # the location check looks at it again on its next pass
        store.event(conn, key, "misplaced", f"Location looks wrong: {queue[i]['title']} (on the map at {queue[i].get('place')})", queue[i]["id"])
        report.setdefault("misplaced", []).append(queue[i]["title"])
    conn.commit()

    targets = list(dict.fromkeys(review["important"] + [i for i in review["follow"]] +
                                 [i for i, s in enumerate(queue) if s["followed"] and s["new_items"]]))
    for i in targets[: int(cfg.get("max_briefs_per_run", 4))]:
        report["briefs"].append(_brief_and_link(conn, llm, agent, name, queue[i]["id"], exclude_desk=desk))
    return report


def _brief_and_link(conn, llm: LLM, agent: dict, desk_name: str, story_id: int, exclude_desk: str | None) -> dict:
    key = agent["key"]
    digest = store.story_digest(conn, story_id)
    b = write_brief(llm, desk_name, digest)
    body = b["brief"] + ("\n\n" + "\n".join(f"- {p}" for p in b["key_points"]) if b["key_points"] else "") + \
        (f"\n\nWatch for: {b['watch_for']}" if b.get("watch_for") else "")
    store.add_brief(conn, "story", key, body, story_id=story_id, title=digest["title"],
                    confidence=b.get("confidence"), escalate=bool(b.get("escalate")))
    store.event(conn, key, "brief", f"Brief: {digest['title']}", story_id)
    if b.get("escalate"):
        store.event(conn, key, "escalation", f"{desk_name} flags for the Editor in Chief: {digest['title']}", story_id)
    out = {"story_id": story_id, "title": digest["title"], "brief": b["brief"], "escalate": bool(b.get("escalate")), "links": []}
    candidates = store.similar_stories(conn, story_id, exclude_desk)
    for l in find_links(llm, desk_name, {**digest, "brief_text": b["brief"]}, candidates):
        other = candidates[l["candidate"]]
        if store.add_link(conn, story_id, other["id"], l["relation"], l["reason"], key):
            out["links"].append({"to": other["title"], "relation": l["relation"], "reason": l["reason"]})
            store.event(conn, key, "link", f"{digest['title']} {l['relation'].replace('_', ' ')} {other['title']}: {l['reason']}", story_id)
    conn.commit()
    return out


def run_surge(conn: psycopg.Connection, agent: dict, llm: LLM) -> dict:
    cfg = _cfg().get("surge") or {}
    story_id = agent["focus_story_id"]
    since = agent["last_run_at"] or agent["created_at"]
    new_items = conn.execute("SELECT count(*) n FROM items WHERE story_id = %s AND collected_at > %s",
                             (story_id, since)).fetchone()["n"] if story_id else 0
    meta = dict(agent["meta"] or {})
    meta["quiet_runs"] = meta.get("quiet_runs", 0) + 1 if new_items < int(cfg.get("quiet_items", 2)) else 0
    age_h = (datetime.now(timezone.utc) - agent["created_at"]).total_seconds() / 3600
    report = {"story_id": story_id, "new_items": new_items, "briefs": [], "retire": False}
    if story_id and (new_items or not meta.get("briefed")):
        report["briefs"].append(_brief_and_link(conn, llm, agent, f"surge team on story {story_id}", story_id, exclude_desk=None))
        meta["briefed"] = True
    report["summary"] = f"{new_items} new articles since the last check."
    if not story_id or meta["quiet_runs"] >= int(cfg.get("retire_after_quiet_runs", 3)) or age_h >= float(cfg.get("max_hours", 72)):
        report["retire"] = True
        report["summary"] += " The story has gone quiet, so this surge agent is retiring and handing back to the desk."
    conn.execute("UPDATE newsroom_agents SET meta = %s WHERE key = %s", (Jsonb(meta), agent["key"]))
    conn.commit()
    return report


def run_directive(conn: psycopg.Connection, agent: dict, llm: LLM, title: str, description: str) -> dict:
    question = f"{title}\n{description}".strip()
    hits = store.search_stories(conn, question, limit=6)
    digests = [store.story_digest(conn, h["id"], max_items=8) for h in hits if h["similarity"] > 0.3]
    a = answer_directive(llm, _desk_name(agent["desk"]), question, digests)
    known = {d["id"] for d in digests}
    for sid in a.get("follow_ids") or []:
        if sid in known:
            store.follow(conn, sid, agent["key"], f"asked: {title[:80]}")
    store.add_brief(conn, "answer", agent["key"], a["answer"], title=title, story_ids=[s for s in a.get("story_ids") or [] if s in known])
    store.event(conn, agent["key"], "brief", f"Answered: {title}")
    conn.commit()
    return {"answer": a["answer"], "stories": [d["title"] for d in digests if d["id"] in (a.get("story_ids") or [])]}


def maybe_standup(conn: psycopg.Connection, agent: dict, llm: LLM, summary: str) -> str | None:
    st = conn.execute(
        """SELECT * FROM standups s WHERE status = 'collecting'
           AND NOT EXISTS (SELECT 1 FROM standup_reports r WHERE r.standup_id = s.id AND r.agent_key = %s)
           ORDER BY created_at LIMIT 1""", (agent["key"],)).fetchone()
    if not st:
        return None
    briefs = conn.execute("SELECT title, body FROM briefs WHERE agent_key = %s AND created_at > now() - interval '24 hours' "
                          "ORDER BY created_at DESC LIMIT 8", (agent["key"],)).fetchall()
    body = standup_report(llm, _desk_name(agent["desk"]), summary, briefs, st["topic"])
    conn.execute("INSERT INTO standup_reports (standup_id, agent_key, body) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                 (st["id"], agent["key"], body))
    conn.commit()
    return body


# --- the heartbeat endpoint ---------------------------------------------------------------

def report_markdown(agent: dict, report: dict, standup: str | None = None) -> str:
    kind = "answer" if report.get("answer") else "check in"
    lines = [f"**{agent['name']}** {kind}. {report.get('summary', '')}".strip()]
    if report.get("stories_reviewed") is not None:
        lines.append(f"\nReviewed {report['stories_reviewed']} stories.")
    for b in report.get("briefs", []):
        lines.append(f"\n### {b['title']}\n{b['brief']}" + (" **Flagged for the Editor in Chief.**" if b.get("escalate") else ""))
        for l in b.get("links", []):
            lines.append(f"- Connects to *{l['to']}* ({l['relation'].replace('_', ' ')}): {l['reason']}")
    if report.get("followed"):
        lines.append("\nNow following: " + "; ".join(report["followed"]))
    if report.get("misplaced"):
        lines.append("\nFlagged as misplaced on the map: " + "; ".join(report["misplaced"]))
    if report.get("misrouted"):
        lines.append("\nSent to cold storage as misrouted: " + "; ".join(report["misrouted"]))
    if report.get("answer"):
        lines.append(f"\n{report['answer']}")
    if standup:
        lines.append(f"\n## Standup report\n{standup}")
    return "\n".join(lines)


def handle_heartbeat(conn: psycopg.Connection, payload: dict, llm: LLM | None = None) -> dict:
    """One Paperclip heartbeat. The task the agent was handed says what kind of run it is:
    "Check in: ..." is the regular review, "Standup #..." is the review plus a standup report,
    anything else assigned to the agent is a question from the Editor in Chief."""
    llm = llm or default_llm()
    agent = store.get_agent(conn, paperclip_id=payload.get("agentId"))
    if not agent or agent["status"] != "active":
        return {"ok": False, "reason": "unknown or retired agent"}
    ctx = payload.get("context") or {}
    run_id = payload.get("runId")
    issue_id = ctx.get("issueId")
    issue = ctx.get("paperclipIssue") or {}
    title = issue.get("title") or ""
    if issue_id and ctx.get("wakeReason") == "finish_successful_run_handoff":
        _report(conn, agent, issue_id, run_id, "Closing: the work for this task was already reported.")
        return {"ok": True, "summary": "handoff closed"}
    is_directive = bool(issue_id) and not title.startswith(("Check in:", "Standup #"))

    try:
        if is_directive:
            report = run_directive(conn, agent, llm, title, issue.get("description") or "")
        elif agent["kind"] == "surge":
            report = run_surge(conn, agent, llm)
        else:
            report = run_desk(conn, agent, llm)
        standup = maybe_standup(conn, agent, llm, report.get("summary", "")) if agent["kind"] == "desk" else None
    except Exception as e:
        conn.rollback()
        store.event(conn, agent["key"], "error", f"{agent['name']} run failed: {e}")
        conn.commit()
        log.exception("agent %s failed", agent["key"])
        _report(conn, agent, issue_id, run_id, f"Run failed: `{type(e).__name__}: {str(e)[:300]}`", status="blocked")
        raise

    md = report_markdown(agent, report, standup)
    conn.execute("UPDATE newsroom_agents SET last_run_at = now(), last_summary = %s WHERE key = %s",
                 (report.get("summary", "")[:1000], agent["key"]))
    store.event(conn, agent["key"], "run", f"{agent['name']}: {report.get('summary', '')}")
    conn.commit()
    _report(conn, agent, issue_id, run_id, md)
    if report.get("retire"):
        from .surge import retire

        retire(conn, agent["key"], report.get("summary", ""))
    return {"ok": True, "summary": report.get("summary")}


def _report(conn, agent: dict, issue_id: str | None, run_id: str | None, body: str, status: str = "done") -> None:
    """Post the report on the task this run was handed, as the agent, and close it. A run with no
    task (a manual wake) reports on the agent's case file instead, through the board."""
    from .paperclip import board

    try:
        if issue_id and agent.get("paperclip_api_key"):
            try:
                Paperclip(token=agent["paperclip_api_key"]).update_issue(issue_id, run_id=run_id, status=status, comment=body,
                                                                         executionPolicy=None)
                return
            except PaperclipError as e:
                log.info("agent could not close %s itself (%s); closing as the board", issue_id, e)
            board(conn).update_issue(issue_id, status=status, comment=body, executionPolicy=None)
        elif agent.get("log_issue_id"):
            board(conn).comment(agent["log_issue_id"], body)
    except (PaperclipError, RuntimeError) as e:
        log.warning("could not report to Paperclip for %s: %s", agent["key"], e)
