"""Keeps the database from growing without limit.

Most of the space goes to embeddings: a 4 KB vector for every article and every story. They are
only needed while a story can still grow and for searches over recent weeks, so after
RETENTION_DAYS (default 30) Wassup drops them. Articles, headlines, places and briefs are all
kept; only the vectors go. Stories you gave feedback on keep theirs, because your feedback
profile is built from them. Old newsroom activity is trimmed after 90 days.

The freed space is reused by new data, so the files stop growing rather than shrinking.
"""
from __future__ import annotations

import logging
import os
import time

import psycopg

log = logging.getLogger(__name__)


def retention_days() -> int:
    # Clustering looks back 3 days and desk searches 2 weeks; never cut inside that.
    return max(15, int(os.environ.get("RETENTION_DAYS", "30")))


def run_retention(conn: psycopg.Connection, batch: int = 20000, max_seconds: float = 120) -> int:
    days = retention_days()
    started, done = time.time(), 0
    steps = [
        """UPDATE items SET embedding = NULL WHERE id IN (
             SELECT id FROM items WHERE embedding IS NOT NULL AND published_at < now() - %(d)s * interval '1 day' LIMIT %(n)s)""",
        """UPDATE stories SET centroid = NULL WHERE id IN (
             SELECT id FROM stories s WHERE centroid IS NOT NULL AND last_seen < now() - %(d)s * interval '1 day'
               AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.story_id = s.id) LIMIT %(n)s)""",
    ]
    for sql in steps:
        while time.time() - started < max_seconds:
            n = conn.execute(sql, {"d": days, "n": batch}).rowcount
            conn.commit()
            done += n
            if n < batch:
                break
    n = conn.execute("DELETE FROM newsroom_events WHERE created_at < now() - interval '90 days'").rowcount
    conn.commit()
    done += n
    if done:
        log.info("retention: cleared %d old vectors and events (older than %d days)", done, days)
    return done
