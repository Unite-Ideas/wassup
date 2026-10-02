"""Embeds new items and groups them into stories.

Each item joins the most similar story seen in the last few days if it is similar enough,
otherwise it starts a new story. The active story centroids live in memory (exact search, no
index tuning), backed by the stories table.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import psycopg

from .config import settings
from .db import kv_get, kv_set
from .embed import DIM, as_array, get_embedder

log = logging.getLogger(__name__)


class ActiveIndex:
    """In memory matrix of recent story centroids for exact nearest neighbour search.
    Storage grows by doubling so adding a story is cheap."""

    def __init__(self):
        self.ids: list[int] = []
        self.pos: dict[int, int] = {}
        self._vecs = np.zeros((1024, DIM), dtype=np.float32)
        self._counts = np.zeros(1024, dtype=np.int64)
        self._seen = np.zeros(1024, dtype=np.float64)
        self.loaded_at = 0.0

    @property
    def size(self) -> int:
        return len(self.ids)

    def _grow(self, need: int) -> None:
        cap = len(self._counts)
        if need <= cap:
            return
        while cap < need:
            cap *= 2
        for name in ("_vecs", "_counts", "_seen"):
            old = getattr(self, name)
            new = np.zeros((cap,) + old.shape[1:], dtype=old.dtype)
            new[: len(old)] = old
            setattr(self, name, new)

    def load(self, conn: psycopg.Connection, window_h: float) -> None:
        rows = conn.execute(
            "SELECT id, centroid, item_count, last_seen FROM stories WHERE last_seen > now() - %s * interval '1 hour'",
            (window_h,),
        ).fetchall()
        self.ids, self.pos = [], {}
        self._grow(max(len(rows), 1))
        for r in rows:
            self._put(r["id"], as_array(r["centroid"]), r["item_count"], r["last_seen"].timestamp())
        self.loaded_at = time.time()
        log.info("active index: %d stories", self.size)

    def _put(self, sid: int, vec: np.ndarray, count: int, seen: float) -> None:
        i = self.size
        self._grow(i + 1)
        self.ids.append(sid)
        self.pos[sid] = i
        self._vecs[i] = vec
        self._counts[i] = count
        self._seen[i] = seen

    def nearest(self, vec: np.ndarray, not_before: float) -> tuple[int | None, float]:
        n = self.size
        if not n:
            return None, -1.0
        sims = self._vecs[:n] @ vec
        sims[self._seen[:n] < not_before] = -1.0
        i = int(np.argmax(sims))
        return self.ids[i], float(sims[i])

    def vector(self, sid: int) -> np.ndarray:
        return self._vecs[self.pos[sid]].copy()

    def add(self, sid: int, vec: np.ndarray, seen: float) -> None:
        self._put(sid, vec, 1, seen)

    def update(self, sid: int, vec: np.ndarray, seen: float) -> np.ndarray:
        i = self.pos[sid]
        c = self._vecs[i] * self._counts[i] + vec
        c /= np.linalg.norm(c) or 1.0
        self._vecs[i] = c
        self._counts[i] += 1
        self._seen[i] = max(self._seen[i], seen)
        return c.copy()


_index = ActiveIndex()


def embed_text(title: str, summary: str | None) -> str:
    return f"{title}. {(summary or '')[:300]}".strip()


def process_new(conn: psycopg.Connection, batch: int = 256, max_seconds: float = 60) -> int:
    """Embed and cluster pending items. Returns how many were processed."""
    try:
        return _process(conn, batch, max_seconds)
    except Exception:
        conn.rollback()
        _index.loaded_at = 0  # the in memory index may be ahead of the database; reload next time
        raise


def _check_embedder(conn: psycopg.Connection, name: str) -> None:
    """Vectors from different embedders cannot be compared, so a database sticks to one."""
    used = kv_get(conn, "embedder")
    if used is None:
        kv_set(conn, "embedder", name)
        conn.commit()
    elif used != name:
        raise RuntimeError(f"stories were built with the '{used}' embedder but '{name}' is configured. "
                           "Switch back, or run `wassup rebuild-stories` to re-cluster everything with the new one.")


def _process(conn: psycopg.Connection, batch: int, max_seconds: float) -> int:
    s = settings()
    threshold, _ = s.thresholds()
    window = s.cluster_window_hours
    if not _index.loaded_at or time.time() - _index.loaded_at > 3600:
        _index.load(conn, window)
    embedder = get_embedder()
    _check_embedder(conn, embedder.id)
    started, done = time.time(), 0

    while time.time() - started < max_seconds:
        rows = conn.execute(
            "SELECT id, title, summary, published_at FROM items WHERE status = 'new' ORDER BY published_at LIMIT %s",
            (batch,),
        ).fetchall()
        if not rows:
            break
        vecs = embedder.embed([embed_text(r["title"], r["summary"]) for r in rows])
        # Reserve ids for any new stories up front so all writes can be batched.
        spare = [x["id"] for x in conn.execute(
            "SELECT nextval('stories_id_seq') AS id FROM generate_series(1, %s)", (len(rows),)).fetchall()]
        new_stories, centroids, item_updates = [], {}, []
        bad = [r["id"] for r, v in zip(rows, vecs) if not v.any()]
        if bad:
            conn.execute("UPDATE items SET status = 'error' WHERE id = ANY(%s)", (bad,))
        for r, v in zip(rows, vecs):
            if not v.any():
                continue
            seen = r["published_at"].timestamp()
            sid, sim = _index.nearest(v, not_before=seen - window * 3600)
            if sid is not None and sim >= threshold:
                centroids[sid] = _index.update(sid, v, seen)
            else:
                sid = spare.pop()
                _index.add(sid, v, seen)
                new_stories.append([sid, r["title"], r["published_at"]])
            item_updates.append((v, sid, r["id"]))
        new_ids = {ns[0] for ns in new_stories}
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO stories (id, title, centroid, item_count, first_seen, last_seen) VALUES (%s, %s, %s, 1, %s, %s)",
                [(sid, title, _index.vector(sid), t, t) for sid, title, t in new_stories])
            cur.executemany("UPDATE stories SET centroid = %s WHERE id = %s",
                            [(c, sid) for sid, c in centroids.items() if sid not in new_ids])
            cur.executemany("UPDATE items SET embedding = %s, story_id = %s, status = 'clustered' WHERE id = %s", item_updates)
        touched = new_ids | set(centroids)
        refresh_stories(conn, list(touched))
        conn.commit()
        done += len(rows)

    if done:
        log.info("clustered %d items", done)
    return done


def refresh_stories(conn: psycopg.Connection, ids: list[int]) -> None:
    """Recompute counts, time span, locations and headline for the given stories."""
    if not ids:
        return
    conn.execute(
        """WITH s AS (
             SELECT story_id, count(*) n, count(DISTINCT coalesce(outlet, source_id::text)) srcs,
                    count(DISTINCT language) langs, min(published_at) f, max(published_at) l
             FROM items WHERE story_id = ANY(%(ids)s) GROUP BY story_id)
           UPDATE stories st SET item_count = s.n, source_count = s.srcs, language_count = s.langs,
                  first_seen = s.f, last_seen = s.l, updated_at = now()
           FROM s WHERE st.id = s.story_id""",
        {"ids": ids},
    )
    conn.execute("DELETE FROM story_places WHERE story_id = ANY(%s)", (ids,))
    conn.execute(
        """INSERT INTO story_places (story_id, place_id, weight)
           SELECT i.story_id, ip.place_id, count(*) + 2 * count(*) FILTER (WHERE ip.in_title)
           FROM items i JOIN item_places ip ON ip.item_id = i.id
           WHERE i.story_id = ANY(%s) GROUP BY 1, 2""",
        (ids,),
    )
    conn.execute(
        """WITH best AS (
             SELECT DISTINCT ON (sp.story_id) sp.story_id, p.id place_id, p.lat, p.lon
             FROM story_places sp JOIN places p ON p.id = sp.place_id
             WHERE sp.story_id = ANY(%(ids)s)
             ORDER BY sp.story_id, sp.weight DESC, (p.kind = 'country'), p.id),
           cc AS (
             SELECT sp.story_id, count(DISTINCT p.country) n FROM story_places sp JOIN places p ON p.id = sp.place_id
             WHERE sp.story_id = ANY(%(ids)s) GROUP BY 1)
           UPDATE stories st SET primary_place_id = best.place_id, lat = best.lat, lon = best.lon,
                  country_count = coalesce(cc.n, 0)
           FROM best LEFT JOIN cc ON cc.story_id = best.story_id WHERE st.id = best.story_id""",
        {"ids": ids},
    )
    # Headline from the most trusted outlet, earliest first. State media headlines are used
    # only when nothing else covers the story.
    conn.execute(
        """WITH t AS (
             SELECT DISTINCT ON (i.story_id) i.story_id, i.title, coalesce(i.outlet_tier, s.trust_tier) tier
             FROM items i JOIN sources s ON s.id = i.source_id
             WHERE i.story_id = ANY(%s)
             ORDER BY i.story_id, array_position(ARRAY['A','B','U','C','S'], coalesce(i.outlet_tier, s.trust_tier)::text),
                      i.published_at)
           UPDATE stories st SET title = t.title, title_tier = t.tier FROM t WHERE st.id = t.story_id""",
        (ids,),
    )


def rebuild_stories(conn: psycopg.Connection) -> int:
    """Throw away all stories and links and cluster every item again. Feedback is lost."""
    conn.execute("TRUNCATE stories RESTART IDENTITY CASCADE")
    n = conn.execute("UPDATE items SET status = 'new', story_id = NULL, embedding = NULL").rowcount
    conn.execute("DELETE FROM kv WHERE key = 'embedder'")
    conn.commit()
    _index.loaded_at = 0
    return n
