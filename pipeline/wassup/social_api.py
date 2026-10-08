"""The SOURCES view: social channels, their scores, and your overrides."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .social import media as media_files
from .social.telegram import handle_of

router = APIRouter(prefix="/api/social")
media_router = APIRouter(prefix="/api/media")


@media_router.get("/{item_id}/{n}")
def media(item_id: int, n: int) -> FileResponse:
    """A photo saved from a post (social/media.py)."""
    with db.connect() as conn:
        row = conn.execute("SELECT path FROM item_media WHERE item_id = %s AND n = %s", (item_id, n)).fetchone()
    root = media_files.media_dir().resolve()
    path = (root / row["path"]).resolve() if row else None
    if path is None or root not in path.parents or not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@router.get("/accounts")
def accounts() -> list[dict]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT a.id, a.platform, a.handle, a.name, a.status, a.pinned, a.banned, a.added_by, a.discovered_from,
                      a.desk, a.kind, a.lean, a.subscribers, a.score, a.stats, a.status_reason, a.status_changed_at,
                      a.last_checked_at, a.last_error, a.created_at, a.via,
                      (SELECT max(i.published_at) FROM items i WHERE i.source_id = a.source_id) AS last_post_at
               FROM social_accounts a ORDER BY (a.status = 'following') DESC, (a.status = 'candidate') DESC,
                      a.score DESC NULLS LAST, a.handle""").fetchall()


class AddIn(BaseModel):
    handle: str
    desk: str | None = None


@router.post("/accounts")
def add(body: AddIn) -> dict:
    """Follow a channel yourself: a handle (@name or name) or a t.me link."""
    raw = body.handle.strip().lstrip("@")
    h = handle_of(raw if raw.startswith("http") else f"https://t.me/{raw}")
    if not h:
        raise HTTPException(400, "not a Telegram channel name or link")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO social_accounts (platform, handle, status, added_by, desk, status_reason)
               VALUES ('telegram', %s, 'following', 'you', %s, 'Added by you.')
               ON CONFLICT (platform, handle) DO UPDATE SET status = 'following', banned = false, added_by = 'you',
                 status_reason = 'Added by you.', status_changed_at = now(), next_check_at = now()""",
            (h, body.desk))
        conn.commit()
    return {"ok": True, "handle": h}


class ActionIn(BaseModel):
    action: str  # follow | pause | pin | unpin | ban | unban


@router.post("/accounts/{account_id}")
def act(account_id: int, body: ActionIn) -> dict:
    sql = {
        "follow": "status = 'following', status_reason = 'Followed by you.', status_changed_at = now(), next_check_at = now()",
        "pause": "status = 'paused', status_reason = 'Paused by you.', status_changed_at = now()",
        "pin": "pinned = true, banned = false, status = 'following', status_reason = 'Pinned by you.', status_changed_at = now(), next_check_at = now()",
        "unpin": "pinned = false",
        "ban": "banned = true, pinned = false, status = 'removed', status_reason = 'Banned by you.', status_changed_at = now()",
        "unban": "banned = false, status = 'candidate', status_reason = 'Unbanned by you.', status_changed_at = now(), next_check_at = now()",
    }.get(body.action)
    if not sql:
        raise HTTPException(400, "unknown action")
    with db.connect() as conn:
        n = conn.execute(f"UPDATE social_accounts SET {sql} WHERE id = %s", (account_id,)).rowcount
        conn.commit()
    if not n:
        raise HTTPException(404, "channel not found")
    return {"ok": True}
