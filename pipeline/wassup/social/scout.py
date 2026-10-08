"""The scout: decides which channels are worth following.

Every hour each channel is scored on its last 30 days of posts, using what the pipeline already
knows. A post joins a story like any article does, so for each post we can ask:

- Corroborated: did at least two independent outlets (news sites, not other channels, which
  copy each other) report the same story within 48 hours? Posts younger than 48 hours that are
  not corroborated yet are left out of this until they are old enough to judge.
- Early: of the corroborated posts, how many came at least 10 minutes before the first
  independent outlet? And by how much (the median lead)?
- Relevant: how many posts ended up in stories a desk tracks?
- Original: how many are the channel's own, rather than forwarded from another?

Counts are smoothed (a channel with 3 lucky posts does not top the table), then weighted into a
score from 0 to 100: corroboration 35, earliness 35, relevance 20, originality 10.

Discovery: a channel that the followed channels forward or link to from at least two of them,
or forward four times, becomes a candidate. Candidates are checked every two hours and scored the
same way. After a week with enough posts, a candidate that scores well is followed; after two
weeks, one that does not is dropped. A followed channel that keeps scoring badly is paused, and
removed a month later. Channels you pinned or added yourself are never paused, and banned ones
are never seen again.
"""
from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Jsonb

from ..config import load_yaml
from ..newsroom import store

log = logging.getLogger(__name__)

DEFAULTS = {"promote_at": 45, "drop_below": 25, "candidate_days": 7, "candidate_min_posts": 15,
            "judge_min_evaluable": 20, "seed_grace_days": 30, "max_following": 300, "max_candidates": 150}


def _cfg() -> dict:
    c = load_yaml("social.yaml").get("telegram") or {}
    return {k: c.get(k, v) for k, v in DEFAULTS.items()}


STATS = """
WITH posts AS (
    SELECT i.id, i.story_id, i.published_at, i.meta->>'forwarded_from' IS NOT NULL AS forwarded, s.routed
    FROM items i JOIN stories s ON s.id = i.story_id
    WHERE i.source_id = %(src)s AND i.published_at > now() - interval '30 days'),
ind AS (  -- independent coverage of the same stories: not social, not this channel
    SELECT p.id, min(o.published_at) AS first_ind,
           count(DISTINCT coalesce(o.outlet, o.source_id::text)) FILTER (WHERE o.published_at <= p.published_at + interval '48 hours') AS outlets
    FROM posts p JOIN items o ON o.story_id = p.story_id AND o.id <> p.id
    JOIN sources so ON so.id = o.source_id AND so.kind <> 'telegram'
    GROUP BY p.id)
SELECT count(*) AS posts,
       count(*) FILTER (WHERE p.routed) AS routed,
       count(*) FILTER (WHERE p.forwarded) AS forwarded,
       count(*) FILTER (WHERE coalesce(ind.outlets, 0) >= 2) AS corroborated,
       count(*) FILTER (WHERE coalesce(ind.outlets, 0) >= 2 OR p.published_at < now() - interval '48 hours') AS evaluable,
       count(*) FILTER (WHERE coalesce(ind.outlets, 0) >= 2 AND ind.first_ind - p.published_at >= interval '10 minutes') AS early,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM ind.first_ind - p.published_at) / 60)
           FILTER (WHERE coalesce(ind.outlets, 0) >= 2 AND ind.first_ind > p.published_at) AS median_lead_min
FROM posts p LEFT JOIN ind ON ind.id = p.id"""


def score(st: dict) -> float:
    posts = st["posts"] or 0
    corr = (st["corroborated"] + 1) / (st["evaluable"] + 4)
    early = (st["early"] + 0.5) / (st["corroborated"] + 3)
    rel = (st["routed"] + 1) / (posts + 3)
    orig = 1 - (st["forwarded"] / posts if posts else 0)
    return round(100 * (0.35 * corr + 0.35 * early + 0.2 * rel + 0.1 * orig), 1)


def run_scout(conn: psycopg.Connection) -> int:
    c = _cfg()
    changed = 0
    accounts = conn.execute("SELECT * FROM social_accounts WHERE platform = 'telegram' AND NOT banned AND status <> 'removed'").fetchall()
    for a in accounts:
        if a["source_id"] is None:
            continue
        st = dict(conn.execute(STATS, {"src": a["source_id"]}).fetchone())
        st["median_lead_min"] = round(st["median_lead_min"], 1) if st["median_lead_min"] is not None else None
        sc = score(st)
        conn.execute("UPDATE social_accounts SET score = %s, stats = %s WHERE id = %s", (sc, Jsonb(st), a["id"]))
        changed += _decide(conn, a, st, sc, c)
    conn.commit()
    changed += _discover(conn, c)
    conn.commit()
    if changed:
        log.info("scout: %d channel changes", changed)
    return changed


def _set(conn, a: dict, status: str, reason: str) -> int:
    conn.execute("UPDATE social_accounts SET status = %s, status_reason = %s, status_changed_at = now(), next_check_at = now() WHERE id = %s",
                 (status, reason, a["id"]))
    store.event(conn, None, "source", f"Telegram @{a['handle']} ({a['name'] or a['handle']}): {status}. {reason}")
    return 1


def _decide(conn, a: dict, st: dict, sc: float, c: dict) -> int:
    age_days = conn.execute("SELECT extract(epoch FROM now() - %s) / 86400 AS d", (a["created_at"],)).fetchone()["d"]
    protected = a["pinned"] or a["added_by"] == "you"
    why = (f"score {sc:.0f}: {st['corroborated']} of {st['evaluable']} posts corroborated, {st['early']} early"
           + (f" (median {st['median_lead_min']:.0f} min ahead)" if st["median_lead_min"] else "") + f", {st['routed']} of {st['posts']} on tracked stories")
    if a["status"] == "candidate":
        if age_days >= c["candidate_days"] and st["posts"] >= c["candidate_min_posts"] and sc >= c["promote_at"]:
            following = conn.execute("SELECT count(*) n FROM social_accounts WHERE status = 'following'").fetchone()["n"]
            if following < c["max_following"]:
                return _set(conn, a, "following", f"Promoted, {why}.")
        if age_days >= 2 * c["candidate_days"] and (sc < c["drop_below"] or st["posts"] < 5) and not protected:
            return _set(conn, a, "removed", f"Not worth following, {why}.")
    elif a["status"] == "following" and not protected:
        grace = c["seed_grace_days"] if a["added_by"] == "seed" else c["candidate_days"]
        if age_days >= grace and st["evaluable"] >= c["judge_min_evaluable"] and sc < c["drop_below"]:
            return _set(conn, a, "paused", f"Paused, {why}.")
    elif a["status"] == "paused" and not protected:
        days = conn.execute("SELECT extract(epoch FROM now() - %s) / 86400 AS d", (a["status_changed_at"],)).fetchone()["d"]
        if days >= 30:
            return _set(conn, a, "removed", "Paused for a month.")
    return 0


DISCOVER = """
SELECT m.handle, count(DISTINCT m.from_account) AS sources, sum(m.n) FILTER (WHERE m.kind = 'forward') AS forwards,
       sum(m.n) AS mentions, array_agg(DISTINCT a.handle) AS via, mode() WITHIN GROUP (ORDER BY a.desk) AS desk
FROM social_mentions m JOIN social_accounts a ON a.id = m.from_account AND a.status = 'following'
WHERE m.platform = 'telegram' AND m.last_seen > now() - interval '14 days'
  AND NOT EXISTS (SELECT 1 FROM social_accounts x WHERE x.platform = 'telegram' AND x.handle = m.handle)
GROUP BY m.handle
HAVING count(DISTINCT m.from_account) >= 2 OR coalesce(sum(m.n) FILTER (WHERE m.kind = 'forward'), 0) >= 4
ORDER BY count(DISTINCT m.from_account) DESC, sum(m.n) DESC LIMIT 10"""


def _discover(conn, c: dict) -> int:
    room = c["max_candidates"] - conn.execute("SELECT count(*) n FROM social_accounts WHERE status = 'candidate'").fetchone()["n"]
    added = 0
    for r in conn.execute(DISCOVER).fetchall()[:max(0, room)]:
        via = ", ".join(f"@{h}" for h in r["via"][:3])
        conn.execute(
            """INSERT INTO social_accounts (platform, handle, status, added_by, discovered_from, desk, status_reason)
               VALUES ('telegram', %s, 'candidate', 'discovered', %s, %s, %s) ON CONFLICT DO NOTHING""",
            (r["handle"], via, r["desk"],
             f"Found through {via}: {r['forwards'] or 0} forwards, {r['mentions']} mentions from {r['sources']} followed channels."))
        store.event(conn, None, "source", f"Telegram @{r['handle']}: new candidate, found through {via}.")
        added += 1
    return added
