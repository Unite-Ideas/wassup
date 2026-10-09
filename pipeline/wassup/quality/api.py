"""The QUALITY tab, the Standards Editor's audit sheet, and your reports from the story panel."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .. import db
from ..db import kv_set
from . import audit

router = APIRouter(prefix="/api")


@router.get("/quality")
def quality(days: int = 30) -> dict:
    from .run import standards_ready

    with db.connect() as conn:
        out = audit.scorecard(conn, max(1, min(days, 365)))
        out["open"] = conn.execute("""SELECT a.id, a.judge, a.created_at, count(c.*) FILTER (WHERE c.verdict IS NULL) AS left
                                      FROM audits a JOIN audit_checks c ON c.audit_id = a.id WHERE a.status = 'open'
                                      GROUP BY a.id ORDER BY a.id DESC LIMIT 1""").fetchone()
        out["standards_editor"] = standards_ready(conn)
    return out


@router.post("/quality/audit")
def audit_now() -> dict:
    with db.connect() as conn:
        kv_set(conn, "audit_now", True)
        conn.commit()
    return {"ok": True}


@router.get("/audits/{audit_id}/sheet", response_class=PlainTextResponse)
def audit_sheet(audit_id: int) -> str:
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM audits WHERE id = %s", (audit_id,)).fetchone():
            raise HTTPException(404, "no such audit")
        return audit.sheet(conn, audit_id)


class Verdict(BaseModel):
    id: int
    verdict: str
    answer: str | None = None
    note: str | None = None


class VerdictsIn(BaseModel):
    verdicts: list[Verdict]


@router.post("/audits/{audit_id}/verdicts")
def audit_verdicts(audit_id: int, body: VerdictsIn) -> dict:
    with db.connect() as conn:
        judge = (conn.execute("SELECT judge FROM audits WHERE id = %s", (audit_id,)).fetchone() or {}).get("judge")
        if not judge:
            raise HTTPException(404, "no such audit")
        by_id = {r["id"]: r for r in conn.execute("SELECT * FROM audit_checks WHERE audit_id = %s AND id = ANY(%s)",
                                                  (audit_id, [v.id for v in body.verdicts]))}
        fixed = 0
        for v in body.verdicts:
            if v.id in by_id:
                fixed += audit.apply_verdict(conn, by_id[v.id], v.verdict, v.answer, v.note, judge)
        conn.commit()
        left = conn.execute("SELECT count(*) AS n FROM audit_checks WHERE audit_id = %s AND verdict IS NULL", (audit_id,)).fetchone()["n"]
    return {"recorded": len([v for v in body.verdicts if v.id in by_id]), "fixed": fixed, "left": left}


@router.post("/audits/{audit_id}/finish")
def audit_finish(audit_id: int) -> dict:
    with db.connect() as conn:
        conn.execute("UPDATE audit_checks SET verdict = 'unsure', note = 'not judged', judged_at = now() WHERE audit_id = %s AND verdict IS NULL",
                     (audit_id,))
        conn.execute("UPDATE audits SET status = 'done', finished_at = now() WHERE id = %s", (audit_id,))
        conn.commit()
    return {"ok": True}


class ReportIn(BaseModel):
    aspect: str              # desk | grouping | importance
    answer: str | None = None  # the right desk key or "none"; too_high or too_low
    item_id: int | None = None  # grouping: the article that does not belong
    note: str | None = None


@router.post("/stories/{story_id}/report")
def report(story_id: int, body: ReportIn) -> dict:
    """You say Wassup got something wrong about a story. It is fixed now and counted on the
    QUALITY scorecard. (Place fixes go through the story's Fix location.)"""
    if body.aspect not in ("desk", "grouping", "importance"):
        raise HTTPException(400, "aspect must be desk, grouping or importance")
    with db.connect() as conn:
        st = conn.execute("SELECT id, desk, coalesce(title_en, title) AS title, significance FROM stories WHERE id = %s", (story_id,)).fetchone()
        if not st:
            raise HTTPException(404, "story not found")
        subject = {"title": st["title"], "desk": st["desk"], "significance": st["significance"]}
        answer = body.answer
        if body.aspect == "grouping":
            item = conn.execute("SELECT id, coalesce(title_en, title) AS t, outlet FROM items WHERE id = %s AND story_id = %s",
                                (body.item_id, story_id)).fetchone()
            if not item:
                raise HTTPException(404, "that article is not in this story")
            subject["articles"] = [{"id": item["id"], "title": item["t"], "outlet": item["outlet"]}]
            answer = "1"
        row = conn.execute("""INSERT INTO audit_checks (aspect, story_id, subject) VALUES (%s, %s, %s) RETURNING *""",
                           (body.aspect, story_id, Jsonb(subject))).fetchone()
        fixed = audit.apply_verdict(conn, row, "wrong", answer, body.note, "you")
        new_story = None
        if body.aspect == "grouping":
            new_story = conn.execute("SELECT story_id FROM items WHERE id = %s", (body.item_id,)).fetchone()["story_id"]
        conn.commit()
    return {"ok": True, "fixed": fixed, "new_story": new_story}
