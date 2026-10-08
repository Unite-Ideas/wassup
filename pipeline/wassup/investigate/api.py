"""The INVESTIGATE view: start an investigation, add links or text, and read what was found."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .. import db
from . import core

router = APIRouter(prefix="/api/investigations")

COUNTS = """
    (SELECT count(*) FROM investigation_sources s WHERE s.investigation_id = v.id AND NOT s.hidden) AS sources,
    (SELECT count(*) FROM investigation_sources s WHERE s.investigation_id = v.id AND s.status = 'analyzed' AND NOT s.hidden) AS relevant,
    (SELECT count(*) FROM investigation_sources s WHERE s.investigation_id = v.id AND s.status IN ('pending', 'fetched')) AS waiting,
    (SELECT count(*) FROM investigation_sources s WHERE s.investigation_id = v.id AND s.status = 'analyzed'
       AND s.analysis->>'account' IN ('eyewitness', 'participant', 'official', 'document')) AS firsthand"""


@router.get("")
def list_all() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(f"""SELECT v.id, v.title, v.brief, v.status, v.watch_until, v.created_at, v.updated_at, v.summary_at,
                                       {COUNTS} FROM investigations v ORDER BY v.status = 'active' DESC, v.updated_at DESC""").fetchall()


class NewIn(BaseModel):
    title: str
    brief: str = ""
    links: str = ""
    watch_days: int = core.WATCH_DAYS


@router.post("")
def create(body: NewIn) -> dict:
    if not body.title.strip():
        raise HTTPException(400, "give the investigation a title")
    with db.connect() as conn:
        return {"id": core.create(conn, body.title, body.brief, body.links, max(1, min(body.watch_days, 365)))}


@router.get("/{inv_id}")
def get(inv_id: int) -> dict:
    with db.connect() as conn:
        inv = conn.execute(f"SELECT v.*, {COUNTS} FROM investigations v WHERE v.id = %s", (inv_id,)).fetchone()
        if not inv:
            raise HTTPException(404, "no such investigation")
        sources = conn.execute(
            """SELECT s.id, s.url, s.kind, s.found_by, s.parent_id, s.depth, s.status, s.title, s.outlet, s.author,
                      s.published_at, s.thumbnail, s.similarity, s.analysis, s.error, s.pinned, s.hidden, s.created_at,
                      s.meta->'partial' AS partial, s.meta->'pasted' AS pasted, s.meta->'duration' AS duration,
                      i.story_id, coalesce(t.chars, 0) AS chars
               FROM investigation_sources s LEFT JOIN items i ON i.id = s.item_id
               LEFT JOIN item_texts t ON t.item_id = s.item_id
               WHERE s.investigation_id = %s ORDER BY s.published_at NULLS LAST, s.id""", (inv_id,)).fetchall()
        lead_rows = conn.execute("SELECT * FROM investigation_leads WHERE investigation_id = %s ORDER BY id", (inv_id,)).fetchall()
    from .agent import investigator_ready
    with db.connect() as conn:
        ready = investigator_ready(conn)
    return {**inv, "sources": sources, "leads": lead_rows, "investigator": ready}


class LinksIn(BaseModel):
    links: str
    found_by: str = "you"  # you | investigator


@router.post("/{inv_id}/links")
def add_links(inv_id: int, body: LinksIn) -> dict:
    urls = core.split_links(body.links)
    if not urls:
        raise HTTPException(400, "no links found in what you pasted")
    with db.connect() as conn:
        _exists(conn, inv_id)
        n = core.add_links(conn, inv_id, urls, _by(body.found_by))
        conn.execute("UPDATE investigations SET status = 'active' WHERE id = %s AND status = 'done'", (inv_id,))
        conn.commit()
    return {"added": n}


class TextIn(BaseModel):
    text: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    found_by: str = "you"


@router.post("/{inv_id}/text")
def add_text(inv_id: int, body: TextIn) -> dict:
    if len(body.text.strip()) < 20:
        raise HTTPException(400, "paste at least a sentence")
    with db.connect() as conn:
        _exists(conn, inv_id)
        sid = core.add_text(conn, inv_id, body.text, (body.url or "").strip() or None, body.title, body.author, body.published_at,
                            _by(body.found_by))
    return {"id": sid}


class EditIn(BaseModel):
    title: str | None = None
    brief: str | None = None
    status: str | None = None      # active | paused | done
    watch_days: int | None = None  # from now


@router.post("/{inv_id}")
def edit(inv_id: int, body: EditIn) -> dict:
    with db.connect() as conn:
        _exists(conn, inv_id)
        if body.title is not None and body.title.strip():
            conn.execute("UPDATE investigations SET title = %s WHERE id = %s", (body.title.strip(), inv_id))
        if body.brief is not None:
            # A new question means new searches and a new summary.
            conn.execute("UPDATE investigations SET brief = %s, queries = '[]', searched_at = NULL, summary_sources = 0 WHERE id = %s",
                         (body.brief.strip(), inv_id))
        if body.status in ("active", "paused", "done"):
            conn.execute("UPDATE investigations SET status = %s WHERE id = %s", (body.status, inv_id))
        if body.watch_days:
            conn.execute("UPDATE investigations SET watch_until = now() + %s * interval '1 day', status = 'active' WHERE id = %s",
                         (max(1, min(body.watch_days, 365)), inv_id))
        conn.execute("UPDATE investigations SET updated_at = now() WHERE id = %s", (inv_id,))
        conn.commit()
    return {"ok": True}


@router.post("/{inv_id}/refresh")
def refresh(inv_id: int) -> dict:
    """Look again now: related reports, a new search, and a new summary."""
    with db.connect() as conn:
        _exists(conn, inv_id)
        conn.execute("""UPDATE investigations SET related_at = NULL, searched_at = NULL, summary_sources = 0, summary_at = NULL,
                        status = CASE WHEN status = 'done' THEN 'active' ELSE status END, updated_at = now() WHERE id = %s""", (inv_id,))
        conn.commit()
    return {"ok": True}


class SourceIn(BaseModel):
    action: str  # hide | unhide | pin | unpin | reread


@router.post("/{inv_id}/sources/{source_id}")
def source_action(inv_id: int, source_id: int, body: SourceIn) -> dict:
    sets = {"hide": "hidden = true", "unhide": "hidden = false", "pin": "pinned = true", "unpin": "pinned = false",
            "reread": "status = CASE WHEN item_id IS NULL THEN 'pending' ELSE 'fetched' END, analysis = NULL, error = NULL"}
    if body.action not in sets:
        raise HTTPException(400, f"action must be one of {', '.join(sets)}")
    with db.connect() as conn:
        n = conn.execute(f"UPDATE investigation_sources SET {sets[body.action]} WHERE id = %s AND investigation_id = %s",
                         (source_id, inv_id)).rowcount
        conn.execute("UPDATE investigations SET summary_sources = 0 WHERE id = %s", (inv_id,))
        conn.commit()
    if not n:
        raise HTTPException(404, "no such source")
    return {"ok": True}


@router.get("/{inv_id}/sources/{source_id}/text")
def source_text(inv_id: int, source_id: int) -> dict:
    with db.connect() as conn:
        src = conn.execute("SELECT * FROM investigation_sources WHERE id = %s AND investigation_id = %s", (source_id, inv_id)).fetchone()
        if not src:
            raise HTTPException(404, "no such source")
        return {"text": core._text_of(conn, src)}


def _exists(conn, inv_id: int) -> None:
    if not conn.execute("SELECT 1 FROM investigations WHERE id = %s", (inv_id,)).fetchone():
        raise HTTPException(404, "no such investigation")


def _by(found_by: str) -> str:
    return "investigator" if found_by == "investigator" else "you"


# --- the Investigator agent (investigate/investigator_instructions.md) --------------------------

@router.get("/{inv_id}/brief", response_class=PlainTextResponse)
def brief(inv_id: int) -> str:
    """Everything about an investigation, as one text, for the Investigator."""
    with db.connect() as conn:
        _exists(conn, inv_id)
        return core.brief_text(conn, inv_id)


@router.get("/{inv_id}/leads")
def leads(inv_id: int) -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM investigation_leads WHERE investigation_id = %s ORDER BY id", (inv_id,)).fetchall()


class LeadIn(BaseModel):
    title: str
    why: str = ""
    how: str = ""
    added_by: str = "investigator"


@router.post("/{inv_id}/leads")
def add_lead(inv_id: int, body: LeadIn) -> dict:
    if not body.title.strip():
        raise HTTPException(400, "a lead needs a title")
    with db.connect() as conn:
        _exists(conn, inv_id)
        row = conn.execute(
            """INSERT INTO investigation_leads (investigation_id, title, why, how, added_by) VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (inv_id, body.title.strip()[:300], body.why.strip(), body.how.strip(), "you" if body.added_by == "you" else "investigator")).fetchone()
        conn.commit()
    return {"id": row["id"]}


class LeadUpdate(BaseModel):
    status: str | None = None
    finding: str | None = None
    urls: list[str] | None = None
    title: str | None = None


@router.post("/{inv_id}/leads/{lead_id}")
def update_lead(inv_id: int, lead_id: int, body: LeadUpdate) -> dict:
    if body.status and body.status not in ("open", "working", "done", "dead_end", "blocked", "dropped"):
        raise HTTPException(400, "status must be open, working, done, dead_end, blocked or dropped")
    with db.connect() as conn:
        n = conn.execute(
            """UPDATE investigation_leads SET status = coalesce(%s, status), finding = coalesce(%s, finding),
                      urls = coalesce(%s, urls), title = coalesce(%s, title), updated_at = now()
               WHERE id = %s AND investigation_id = %s""",
            (body.status, body.finding, Jsonb(body.urls) if body.urls is not None else None, body.title, lead_id, inv_id)).rowcount
        conn.commit()
    if not n:
        raise HTTPException(404, "no such lead")
    return {"ok": True}


class MemoIn(BaseModel):
    body: str


@router.post("/{inv_id}/memo")
def memo(inv_id: int, body: MemoIn) -> dict:
    with db.connect() as conn:
        _exists(conn, inv_id)
        conn.execute("UPDATE investigations SET memo = %s, memo_at = now(), ask_note = NULL, updated_at = now() WHERE id = %s",
                     (body.body, inv_id))
        conn.commit()
    return {"ok": True}


class AskIn(BaseModel):
    note: str = ""


@router.post("/{inv_id}/ask")
def ask(inv_id: int, body: AskIn) -> dict:
    """Ask the Investigator to look at this investigation now, optionally with a note."""
    from .agent import investigator_ready

    with db.connect() as conn:
        _exists(conn, inv_id)
        if not investigator_ready(conn):
            raise HTTPException(409, "The Investigator is not hired yet: run `docker compose exec app wassup newsroom setup`.")
        conn.execute("UPDATE investigations SET ask_note = %s, ask_at = now(), status = 'active' WHERE id = %s",
                     (body.note.strip() or None, inv_id))
        conn.commit()
    return {"ok": True}
