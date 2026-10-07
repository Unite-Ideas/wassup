"""Database helpers for the newsroom."""
from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from ..embed import get_embedder

# Relations an agent can draw between two stories. Rule based links use "related" and
# "same_actor"; agent links use these, so the dashboard can tell them apart.
AGENT_RELATIONS = ["causes", "responds_to", "escalates", "part_of", "contradicts", "parallels"]


def event(conn, agent_key: str | None, kind: str, text: str, story_id: int | None = None, **meta) -> None:
    conn.execute("INSERT INTO newsroom_events (agent_key, kind, text, story_id, meta) VALUES (%s, %s, %s, %s, %s)",
                 (agent_key, kind, text[:2000], story_id, Jsonb(meta)))
    if agent_key == "eic" and kind != "escalation":
        # The Editor in Chief works through the API rather than the heartbeat endpoint, so its
        # latest action is its activity.
        conn.execute("UPDATE newsroom_agents SET last_run_at = now(), last_summary = %s WHERE key = 'eic'", (text[:1000],))


def add_brief(conn, kind: str, agent_key: str, body: str, story_id: int | None = None,
              title: str | None = None, **meta) -> int:
    return conn.execute(
        "INSERT INTO briefs (kind, story_id, agent_key, title, body, meta) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (kind, story_id, agent_key, title, body, Jsonb(meta))).fetchone()["id"]


def latest_brief(conn, story_id: int) -> dict | None:
    return conn.execute("SELECT * FROM briefs WHERE story_id = %s AND kind = 'story' ORDER BY created_at DESC LIMIT 1",
                        (story_id,)).fetchone()


def follow(conn, story_id: int, agent_key: str, reason: str | None = None) -> None:
    conn.execute(
        """INSERT INTO follows (story_id, agent_key, reason) VALUES (%s, %s, %s)
           ON CONFLICT (story_id, agent_key) DO UPDATE SET active = true, reason = coalesce(EXCLUDED.reason, follows.reason)""",
        (story_id, agent_key, reason))


def unfollow(conn, story_id: int, agent_key: str) -> None:
    conn.execute("UPDATE follows SET active = false WHERE story_id = %s AND agent_key = %s", (story_id, agent_key))


def add_link(conn, a: int, b: int, relation: str, reason: str, agent_key: str, weight: float = 0.8) -> bool:
    if a == b or relation not in AGENT_RELATIONS:
        return False
    lo, hi = sorted((a, b))
    # Direction matters for "causes" and "responds_to", so keep which story came first. Either
    # story may have been merged into another while the desk was thinking; then skip the link.
    row = conn.execute(
        """INSERT INTO story_links (a, b, kind, weight, evidence, created_by)
           SELECT %s, %s, %s, %s, %s, %s WHERE (SELECT count(*) FROM stories WHERE id IN (%s, %s)) = 2
           ON CONFLICT (a, b, kind) DO UPDATE SET evidence = EXCLUDED.evidence, weight = EXCLUDED.weight, updated_at = now()
           RETURNING 1""",
        (lo, hi, relation, weight, Jsonb({"reason": reason, "from": a, "to": b, "by": agent_key}), f"agent:{agent_key}", lo, hi)).fetchone()
    return row is not None


def get_agent(conn, key: str | None = None, paperclip_id: str | None = None) -> dict | None:
    if key:
        return conn.execute("SELECT * FROM newsroom_agents WHERE key = %s", (key,)).fetchone()
    return conn.execute("SELECT * FROM newsroom_agents WHERE paperclip_agent_id = %s", (paperclip_id,)).fetchone()


def active_agents(conn, kind: str | None = None) -> list[dict]:
    if kind:
        return conn.execute("SELECT * FROM newsroom_agents WHERE status = 'active' AND kind = %s ORDER BY key", (kind,)).fetchall()
    return conn.execute("SELECT * FROM newsroom_agents WHERE status = 'active' ORDER BY kind, key").fetchall()


def upsert_agent(conn, key: str, kind: str, name: str, **fields) -> None:
    cols = ["key", "kind", "name", *fields]
    vals = [key, kind, name, *[Jsonb(v) if isinstance(v, dict) else v for v in fields.values()]]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
    conn.execute(f"INSERT INTO newsroom_agents ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                 f"ON CONFLICT (key) DO UPDATE SET {updates}", vals)


STORY_FIELDS = """s.id, coalesce(s.title_en, s.title) AS title, s.desk, s.significance, s.item_count, s.source_count,
    s.country_count, s.breaking, s.velocity, s.first_seen, s.last_seen"""


def desk_queue(conn, desk: str, agent_key: str, since: datetime, limit: int) -> list[dict]:
    """Stories on this desk with coverage since the last run, plus stories the desk follows
    that have new coverage, most significant first."""
    return conn.execute(
        f"""SELECT {STORY_FIELDS}, (f.story_id IS NOT NULL) AS followed, pl.name AS place, pl.country AS place_country,
                   (SELECT count(*) FROM items i WHERE i.story_id = s.id AND i.collected_at > %(since)s) AS new_items
            FROM stories s LEFT JOIN follows f ON f.story_id = s.id AND f.agent_key = %(agent)s AND f.active
                 LEFT JOIN places pl ON pl.id = s.primary_place_id
            WHERE s.last_seen > %(since)s AND ((s.routed AND s.desk = %(desk)s) OR f.story_id IS NOT NULL)
            ORDER BY (f.story_id IS NOT NULL) DESC, s.breaking DESC, s.significance DESC, s.item_count DESC
            LIMIT %(limit)s""",
        {"since": since, "agent": agent_key, "desk": desk, "limit": limit}).fetchall()


def story_digest(conn, story_id: int, max_items: int = 14) -> dict:
    """What an agent needs to know about one story, in compact form."""
    s = conn.execute(f"SELECT {STORY_FIELDS} FROM stories s WHERE s.id = %s", (story_id,)).fetchone()
    items = conn.execute(
        """SELECT DISTINCT ON (coalesce(i.title_en, i.title)) coalesce(i.title_en, i.title) AS title, i.summary,
                  coalesce(i.outlet, src.name) AS outlet, coalesce(i.outlet_tier, src.trust_tier) AS tier,
                  (i.outlet_state OR src.state_media) AS state, i.language, i.published_at, t.body
           FROM items i JOIN sources src ON src.id = i.source_id
           LEFT JOIN item_texts t ON t.item_id = i.id AND t.status = 'ok'
           WHERE i.story_id = %s
           ORDER BY coalesce(i.title_en, i.title), (t.body IS NULL), i.published_at DESC""", (story_id,)).fetchall()
    # Trusted outlets first, and among them articles whose full text has been read.
    items = sorted(items, key=lambda r: (r["tier"] == "S", "ACBUS".find(r["tier"] or "U"), r["body"] is None,
                                         -r["published_at"].timestamp()))[:max_items]
    places = [r["name"] for r in conn.execute(
        "SELECT p.name FROM story_places sp JOIN places p ON p.id = sp.place_id WHERE sp.story_id = %s ORDER BY sp.weight DESC LIMIT 6",
        (story_id,))]
    actors = [r["name"] for r in conn.execute(
        """SELECT e.name FROM items i JOIN item_entities ie ON ie.item_id = i.id JOIN entities e ON e.id = ie.entity_id
           WHERE i.story_id = %s GROUP BY e.name ORDER BY count(*) DESC LIMIT 8""", (story_id,))]
    return {**s, "items": items, "places": places, "actors": actors, "brief": latest_brief(conn, story_id)}


def digest_text(d: dict) -> str:
    lines = [f"Story {d['id']}: {d['title']}",
             f"{d['item_count']} articles from {d['source_count']} outlets in {d['country_count']} countries"
             f"{', BREAKING' if d['breaking'] else ''}, first seen {d['first_seen']:%b %d %H:%M}, latest {d['last_seen']:%b %d %H:%M} UTC. Places: {', '.join(d['places']) or 'unknown'}. "
             f"Actors: {', '.join(d['actors']) or 'unknown'}."]
    # The full text of the best few articles, when it has been read; a summary line for the rest.
    texts = 0
    for it in d["items"]:
        flag = " [STATE MEDIA]" if it["state"] else ""
        if it.get("body") and texts < 4:
            texts += 1
            body = " ".join(it["body"].split())[:1500]
            lines.append(f"- ({it['outlet']}, tier {it['tier']}{flag}, {it['published_at']:%b %d %H:%M}) {it['title']}\n  Article: {body}")
            continue
        summary = f" | {it['summary'][:220]}" if it.get("summary") else ""
        lines.append(f"- ({it['outlet']}, tier {it['tier']}{flag}, {it['published_at']:%b %d %H:%M}) {it['title']}{summary}")
    if d.get("brief"):
        lines.append(f"Previous brief ({d['brief']['created_at']:%b %d %H:%M}): {d['brief']['body'][:900]}")
    return "\n".join(lines)


def similar_stories(conn, story_id: int, exclude_desk: str | None, limit: int = 8, hours: int = 96) -> list[dict]:
    """Stories elsewhere in the newsroom that look related, as link candidates."""
    return conn.execute(
        f"""SELECT {STORY_FIELDS}, 1 - (s.centroid <=> t.centroid) AS similarity
            FROM stories s, (SELECT centroid FROM stories WHERE id = %(id)s) t
            WHERE s.routed AND s.id <> %(id)s AND s.last_seen > now() - %(hours)s * interval '1 hour'
              AND (%(desk)s::text IS NULL OR s.desk IS DISTINCT FROM %(desk)s)
            ORDER BY s.centroid <=> t.centroid LIMIT %(limit)s""",
        {"id": story_id, "desk": exclude_desk, "limit": limit, "hours": hours}).fetchall()


def search_stories(conn, text: str, limit: int = 10, days: int = 14, cold: bool = True) -> list[dict]:
    """Stories closest in meaning to a question, including cold storage when asked."""
    vec = get_embedder().embed([text])[0]
    return conn.execute(
        f"""SELECT {STORY_FIELDS}, s.routed, 1 - (s.centroid <=> %(v)s) AS similarity FROM stories s
            WHERE s.last_seen > now() - %(days)s * interval '1 day' AND (%(cold)s OR s.routed)
            ORDER BY s.centroid <=> %(v)s LIMIT %(limit)s""",
        {"v": vec, "days": days, "cold": cold, "limit": limit}).fetchall()
