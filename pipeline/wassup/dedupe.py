"""The same event as two stories: find them and have them merged.

Articles join a story when they are close enough in meaning, and whole stories merge when very
close (cluster.py). Measured on real headlines, stories about the same event often sit just
below that: the death at a Russian plague lab was eight stories, linked to each other as
"related". Every few minutes the closest unmerged pairs are shown to the local model, with a
few headlines each, a batch at a time: same event or not. Same ones are recorded here and
merged by the clustering step on its next pass (it owns the story index); different ones are
remembered so they are not asked about again.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import psycopg

from .embed import as_array

log = logging.getLogger(__name__)

GRAY = 0.68      # from here up to the merge threshold, pairs are asked about
BATCH = 10
PER_RUN = 40
WINDOW_H = 72

SCHEMA = {"type": "object", "properties": {"pairs": {"type": "array", "items": {"type": "object", "properties": {
    "n": {"type": "integer"}, "answer": {"type": "string", "enum": ["same_story", "related", "unrelated"]}},
    "required": ["n", "answer"]}}}, "required": ["pairs"]}


def candidates(conn, limit: int = PER_RUN) -> list[tuple[float, int, int]]:
    from .config import settings

    rows = conn.execute(
        """SELECT id, centroid FROM stories WHERE centroid IS NOT NULL AND last_seen > now() - %s * interval '1 hour'
           ORDER BY routed DESC, significance DESC, item_count DESC LIMIT 4000""", (WINDOW_H,)).fetchall()
    if len(rows) < 2:
        return []
    ids = np.array([r["id"] for r in rows])
    m = np.array([as_array(r["centroid"]) for r in rows])
    sims = m @ m.T
    top = settings().merge_at()
    i, j = np.nonzero(np.triu((sims >= GRAY) & (sims < top + 0.08), 1))
    pairs = sorted(((float(sims[a, b]), int(min(ids[a], ids[b])), int(max(ids[a], ids[b]))) for a, b in zip(i, j)), reverse=True)
    if not pairs:
        return []
    done = {(r["a"], r["b"]) for r in conn.execute(
        """SELECT a, b FROM story_pair_checks WHERE a = ANY(%(ids)s) OR b = ANY(%(ids)s)
           UNION SELECT a, b FROM story_splits WHERE a = ANY(%(ids)s) OR b = ANY(%(ids)s)""", {"ids": [p[1] for p in pairs] + [p[2] for p in pairs]})}
    return [p for p in pairs if (p[1], p[2]) not in done][:limit]


def ask(llm, pairs: list[tuple[float, int, int]], heads: dict[int, list[str]]) -> dict[int, bool]:
    def show(sid):
        return " | ".join(heads.get(sid, [])[:3]) or "?"
    lines = "\n".join(f"[{n + 1}] A: {show(a)}\n    B: {show(b)}" for n, (_, a, b) in enumerate(pairs))
    prompt = f"""You are an editor removing duplicate stories. Each numbered pair is two groups of headlines, A and B.

For each pair answer:
- same_story: A and B are coverage of one news story, so an editor would put them under one headline. Reports of
  the same incident, arrest, statement, decision, death, attack or announcement are the same story, even with
  different details, wording, angles, languages or reactions, and even a day apart.
- related: two different news stories on a connected subject (for example two separate attacks, or two different
  statements on different days).
- unrelated: nothing to do with each other.

{lines}

Answer for every number."""
    out = llm(prompt, SCHEMA)
    return {int(p["n"]): p.get("answer") == "same_story" for p in out.get("pairs") or []
            if isinstance(p.get("n"), int) and 1 <= p["n"] <= len(pairs)}


def run_duplicates(conn: psycopg.Connection, llm=None, max_seconds: float = 120) -> int:
    pairs = candidates(conn)
    conn.commit()
    if not pairs:
        return 0
    if llm is None:
        from .newsroom.llm import default_llm
        llm = default_llm()
    sids = sorted({p[1] for p in pairs} | {p[2] for p in pairs})
    heads: dict[int, list[str]] = {}
    for r in conn.execute(
            """SELECT story_id, coalesce(title_en, title) AS t FROM (
                 SELECT story_id, title_en, title, row_number() OVER (PARTITION BY story_id ORDER BY published_at) rn
                 FROM items WHERE story_id = ANY(%s)) x WHERE rn <= 3""", (sids,)):
        heads.setdefault(r["story_id"], []).append(r["t"])
    conn.commit()
    started, same, checked = time.time(), 0, 0
    for i in range(0, len(pairs), BATCH):
        if time.time() - started > max_seconds:
            break
        batch = pairs[i:i + BATCH]
        try:
            answers = ask(llm, batch, heads)
        except Exception as e:  # noqa: BLE001
            log.warning("duplicate check failed: %s", e)
            break
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO story_pair_checks (a, b, similarity, same) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (a, b) DO UPDATE SET same = EXCLUDED.same, checked_at = now()""",
                [(a, b, sim, answers[n + 1]) for n, (sim, a, b) in enumerate(batch) if n + 1 in answers])
        conn.commit()
        checked += len(answers)
        same += sum(answers.values())
    if checked:
        log.info("duplicate check: %d pairs looked at, %d are the same event (merged at the next clustering pass)", checked, same)
    return checked
