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
from .text import comparable_title

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

    def candidates(self, min_items: int, not_before: float) -> np.ndarray:
        """Positions of live stories with at least min_items articles."""
        n = self.size
        return np.flatnonzero((self._counts[:n] >= min_items) & (self._seen[:n] >= not_before))

    def absorb(self, keep: int, gone: int) -> np.ndarray:
        """Fold story gone into story keep; gone can never be matched again."""
        a, b = self.pos[keep], self.pos[gone]
        c = self._vecs[a] * self._counts[a] + self._vecs[b] * self._counts[b]
        c /= np.linalg.norm(c) or 1.0
        self._vecs[a] = c
        self._counts[a] += self._counts[b]
        self._seen[a] = max(self._seen[a], self._seen[b])
        self._counts[b], self._seen[b] = 0, -np.inf
        return c.copy()

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
    return f"{comparable_title(title)}. {(summary or '')[:300]}".strip()


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
           SELECT i.story_id, ip.place_id, sum(ip.weight)
           FROM items i JOIN item_places ip ON ip.item_id = i.id
           WHERE i.story_id = ANY(%s) GROUP BY 1, 2""",
        (ids,),
    )
    # Main location: the place with the most evidence (cities before countries on a tie). Its
    # share of all the evidence is the confidence, and the source says what backs it. Locked
    # locations (verified, or fixed by you) stay put.
    conn.execute(
        """WITH ev AS (
             SELECT sp.story_id, p.id place_id, p.lat, p.lon, p.kind, sp.weight,
                    sum(sp.weight) OVER (PARTITION BY sp.story_id) total,
                    row_number() OVER (PARTITION BY sp.story_id ORDER BY sp.weight DESC, (p.kind = 'country'), p.id) rn
             FROM story_places sp JOIN places p ON p.id = sp.place_id
             WHERE sp.story_id = ANY(%(ids)s) AND sp.weight > 0.15),
           best AS (SELECT * FROM ev WHERE rn = 1),
           src AS (
             SELECT i.story_id, ip.place_id, bool_or(ip.in_title) in_title,
                    bool_or(i.meta->>'feed' IS NULL) from_text
             FROM items i JOIN item_places ip ON ip.item_id = i.id
             WHERE i.story_id = ANY(%(ids)s) GROUP BY 1, 2),
           cc AS (
             SELECT sp.story_id, count(DISTINCT p.country) n FROM story_places sp JOIN places p ON p.id = sp.place_id
             WHERE sp.story_id = ANY(%(ids)s) AND sp.weight > 0.15 GROUP BY 1)
           UPDATE stories st SET primary_place_id = best.place_id, lat = best.lat, lon = best.lon,
                  country_count = coalesce(cc.n, 0),
                  location_confidence = round((best.weight / nullif(best.total, 0))::numeric, 3),
                  location_source = CASE WHEN src.in_title THEN 'headline' WHEN src.from_text THEN 'text' ELSE 'tagger' END
           FROM best LEFT JOIN cc ON cc.story_id = best.story_id
                LEFT JOIN src ON src.story_id = best.story_id AND src.place_id = best.place_id
           WHERE st.id = best.story_id AND NOT st.location_locked""",
        {"ids": ids},
    )
    conn.execute("""UPDATE stories SET significance = wassup_significance(importance, source_count, country_count)
                    WHERE id = ANY(%s) AND importance IS NOT NULL""", (ids,))
    refresh_headlines(conn, ids)


def refresh_headlines(conn: psycopg.Connection, ids: list[int]) -> None:
    """Headline from the most trusted outlet, preferring one readable in English, earliest
    first. State media headlines are used only when nothing else covers the story."""
    conn.execute(
        """WITH t AS (
             SELECT DISTINCT ON (i.story_id) i.story_id, i.title, coalesce(i.outlet_tier, s.trust_tier) tier,
                    CASE WHEN i.language = 'en' THEN i.title ELSE i.title_en END title_en
             FROM items i JOIN sources s ON s.id = i.source_id
             WHERE i.story_id = ANY(%s)
             ORDER BY i.story_id, array_position(ARRAY['A','B','U','C','S'], coalesce(i.outlet_tier, s.trust_tier)::text),
                      (i.language = 'en' OR i.title_en IS NOT NULL) DESC, i.published_at)
           UPDATE stories st SET title = t.title, title_tier = t.tier, title_en = t.title_en
           FROM t WHERE st.id = t.story_id""",
        (ids,),
    )


def merge_stories(conn: psycopg.Connection, max_merges: int = 500) -> int:
    """Merge stories that turn out to be the same event.

    Articles join a story one at a time, so the same event can start as two stories (say the
    English wire copy and the foreign press) that then grow side by side. Every few minutes,
    stories with new articles are compared with every other live story of two or more
    articles, and any pair closer than the merge threshold becomes one story. The larger story
    keeps its id; the other one's articles, briefs, follows, feedback and links move over.
    """
    try:
        return _merge(conn, max_merges)
    except Exception:
        conn.rollback()
        _index.loaded_at = 0
        raise


def _merge(conn: psycopg.Connection, max_merges: int) -> int:
    s = settings()
    at, window = s.merge_at(), s.cluster_window_hours
    if not _index.loaded_at or time.time() - _index.loaded_at > 3600:
        _index.load(conn, window)
    since = kv_get(conn, "merge_since")
    started = time.time()
    rows = conn.execute(
        "SELECT id FROM stories WHERE item_count >= 2 AND updated_at > coalesce(%s::timestamptz, '-infinity')",
        (since,)).fetchall()
    pool = _index.candidates(2, time.time() - window * 3600)
    live = set(pool.tolist())
    touched = np.array([_index.pos[r["id"]] for r in rows if _index.pos.get(r["id"]) in live], dtype=np.int64)
    pairs = []
    if len(touched) and len(pool):
        pool_vecs = _index._vecs[pool]
        for i in range(0, len(touched), 512):
            chunk = touched[i:i + 512]
            sims = _index._vecs[chunk] @ pool_vecs.T
            sims[pool[None, :] == chunk[:, None]] = -1
            r, c = np.nonzero(sims >= at)
            pairs += [(float(sims[a, b]), int(chunk[a]), int(pool[b])) for a, b in zip(r, c)]
    merged, gone = 0, set()
    for sim, pa, pb in sorted(pairs, reverse=True):
        a, b = _index.ids[pa], _index.ids[pb]
        if a in gone or b in gone or a == b:
            continue
        # Each story may have drifted since this pass started; check again.
        if float(_index._vecs[pa] @ _index._vecs[pb]) < at:
            continue
        keep, drop = (a, b) if (_index._counts[pa], -a) >= (_index._counts[pb], -b) else (b, a)
        _absorb(conn, keep, drop)
        gone.add(drop)
        merged += 1
        if merged >= max_merges:
            break
    kv_set(conn, "merge_since", conn.execute("SELECT now()::text AS t").fetchone()["t"] if merged < max_merges else since)
    conn.commit()
    if merged:
        log.info("merged %d duplicate stories in %.1fs", merged, time.time() - started)
    return merged


def _absorb(conn: psycopg.Connection, keep: int, drop: int) -> None:
    centroid = _index.absorb(keep, drop)
    k, d = (conn.execute("SELECT routed, desk FROM stories WHERE id = %s", (x,)).fetchone() for x in (keep, drop))
    had_feedback = conn.execute("SELECT 1 FROM feedback WHERE story_id = %s LIMIT 1", (drop,)).fetchone()
    p = {"k": keep, "d": drop}
    for sql in (
        "UPDATE items SET story_id = %(k)s WHERE story_id = %(d)s",
        "UPDATE feedback SET story_id = %(k)s WHERE story_id = %(d)s",
        "UPDATE briefs SET story_id = %(k)s WHERE story_id = %(d)s",
        "UPDATE newsroom_events SET story_id = %(k)s WHERE story_id = %(d)s",
        "UPDATE newsroom_agents SET focus_story_id = %(k)s WHERE focus_story_id = %(d)s",
        "UPDATE location_corrections SET story_id = %(k)s WHERE story_id = %(d)s",
        """INSERT INTO follows (story_id, agent_key, reason, active, created_at)
           SELECT %(k)s, agent_key, reason, active, created_at FROM follows WHERE story_id = %(d)s ON CONFLICT DO NOTHING""",
        """INSERT INTO escalations (story_id, paperclip_issue_id, created_at)
           SELECT %(k)s, paperclip_issue_id, created_at FROM escalations WHERE story_id = %(d)s ON CONFLICT DO NOTHING""",
        """INSERT INTO story_links (a, b, kind, weight, evidence, created_by, created_at, updated_at)
           SELECT least(o, %(k)s), greatest(o, %(k)s), kind, weight, evidence, created_by, created_at, updated_at
           FROM (SELECT CASE WHEN a = %(d)s THEN b ELSE a END o, * FROM story_links WHERE %(d)s IN (a, b)) l
           WHERE o <> %(k)s ON CONFLICT DO NOTHING""",
        "DELETE FROM stories WHERE id = %(d)s",
    ):
        conn.execute(sql, p)
    retriage = had_feedback or (k["routed"], k["desk"]) != (d["routed"], d["desk"])
    conn.execute("UPDATE stories SET centroid = %s" + (", triaged_item_count = 0" if retriage else "") + " WHERE id = %s",
                 (centroid, keep))
    refresh_stories(conn, [keep])


def rebuild_stories(conn: psycopg.Connection) -> int:
    """Throw away all stories and links and cluster every item again. Feedback is lost."""
    conn.execute("TRUNCATE stories RESTART IDENTITY CASCADE")
    n = conn.execute("UPDATE items SET status = 'new', story_id = NULL, embedding = NULL").rowcount
    conn.execute("DELETE FROM kv WHERE key = 'embedder'")
    conn.commit()
    _index.loaded_at = 0
    return n
