"""Breaking news detection and story linking (the strings on the wall)."""
from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from .config import settings
from .embed import as_array

log = logging.getLogger(__name__)

# A story is breaking when, in the last hour, it has at least this many new articles from at
# least this many outlets, and that pace is several times its own previous 24 hour average.
BREAKING_MIN_ITEMS = 6
BREAKING_MIN_OUTLETS = 4
BREAKING_RATIO = 3.0


def update_breaking(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        """WITH recent AS (
             SELECT story_id,
                    count(*) FILTER (WHERE published_at > now() - interval '1 hour') last_hour,
                    count(DISTINCT coalesce(outlet, source_id::text)) FILTER (WHERE published_at > now() - interval '1 hour') outlets,
                    count(*) FILTER (WHERE published_at <= now() - interval '1 hour') prior_day
             FROM items WHERE published_at > now() - interval '25 hours' AND story_id IS NOT NULL
             GROUP BY story_id)
           UPDATE stories s SET velocity = r.last_hour,
                  breaking = s.routed AND r.last_hour >= %s AND r.outlets >= %s AND r.last_hour >= %s * greatest(r.prior_day / 24.0, 1)
           FROM recent r WHERE s.id = r.story_id
           RETURNING s.id, s.breaking""",
        (BREAKING_MIN_ITEMS, BREAKING_MIN_OUTLETS, BREAKING_RATIO),
    ).fetchall()
    # Stories with no articles in the last 25 hours are no longer breaking.
    conn.execute("UPDATE stories SET breaking = false, velocity = 0 WHERE (breaking OR velocity > 0) AND last_seen < now() - interval '25 hours'")
    conn.commit()
    n = sum(1 for r in rows if r["breaking"])
    if n:
        log.info("%d breaking stories", n)
    return n


# Organizations that appear in so many unrelated stories that sharing them says nothing.
GENERIC_ENTITIES = {
    "republican party", "democratic party", "republicans", "democrats", "white house", "congress", "senate",
    "house of representatives", "united nations", "european union", "nato", "reuters", "associated press",
    "afp", "agence france presse", "bloomberg", "cnn", "bbc", "fox news", "new york times", "washington post",
    "facebook", "twitter", "instagram", "youtube", "google", "tiktok", "x", "telegram", "whatsapp", "apple",
    "microsoft", "amazon", "government", "parliament", "ministry of foreign affairs", "foreign ministry",
    "supreme court", "police", "army", "military", "pentagon", "state department", "french embassy",
}


def update_links(conn: psycopg.Connection, window_h: float = 72, max_stories: int = 4000,
                 per_story: int = 6, max_entity_df: int = 15) -> int:
    """Link routed stories that are related but distinct (embedding similarity just below the
    clustering threshold) or that share uncommon people and organizations."""
    cluster_t, link_t = settings().thresholds()
    stories = conn.execute(
        """SELECT id, centroid FROM stories WHERE routed AND last_seen > now() - %s * interval '1 hour'
           ORDER BY significance DESC, item_count DESC LIMIT %s""", (window_h, max_stories)).fetchall()
    if len(stories) < 2:
        return 0
    ids = [s["id"] for s in stories]
    m = np.array([as_array(s["centroid"]) for s in stories])
    links: dict[tuple[int, int, str], tuple[float, dict]] = {}

    sims = m @ m.T
    np.fill_diagonal(sims, -1)
    for i in range(len(ids)):
        row = sims[i]
        cand = np.where(row >= link_t)[0]
        for j in cand[np.argsort(-row[cand])][:per_story]:
            a, b = sorted((ids[i], ids[int(j)]))
            links[(a, b, "related")] = (float(row[j]), {"similarity": round(float(row[j]), 3)})

    # Shared entities, ignoring ones that appear everywhere (a head of state shows up in
    # hundreds of unrelated stories).
    ents: dict[int, set[int]] = defaultdict(set)
    names: dict[int, str] = {}
    for r in conn.execute(
        """SELECT DISTINCT i.story_id, e.id, e.name FROM items i JOIN item_entities ie ON ie.item_id = i.id
           JOIN entities e ON e.id = ie.entity_id WHERE i.story_id = ANY(%s)""", (ids,)):
        if r["name"].lower() in GENERIC_ENTITIES:
            continue
        ents[r["id"]].add(r["story_id"])
        names[r["id"]] = r["name"]
    shared: dict[tuple[int, int], list[int]] = defaultdict(list)
    for eid, sids in ents.items():
        if 2 <= len(sids) <= max_entity_df:
            for a, b in combinations(sorted(sids), 2):
                shared[(a, b)].append(eid)
    for (a, b), eids in shared.items():
        if len(eids) >= 2:
            links[(a, b, "same_actor")] = (min(1.0, len(eids) / 4), {"entities": [names[e] for e in eids[:8]]})

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO story_links (a, b, kind, weight, evidence) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (a, b, kind) DO UPDATE SET weight = EXCLUDED.weight, evidence = EXCLUDED.evidence, updated_at = now()""",
            [(a, b, k, w, Jsonb(ev)) for (a, b, k), (w, ev) in links.items()],
        )
    conn.commit()
    log.info("links: %d upserted across %d stories", len(links), len(ids))
    return len(links)
