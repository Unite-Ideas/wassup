"""Postgres access. One connection pool per process."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg.types.json
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import REPO_ROOT, settings

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(settings().database_url, min_size=1, max_size=40, configure=_configure,
                               kwargs={"row_factory": dict_row}, open=True)
    return _pool


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        yield conn


def init_schema(force: bool = False) -> None:
    """Bring the database up to db/schema.sql. Skipped when this exact schema was already applied
    (the app applies it at startup), because its ALTER TABLEs lock tables the running app is using:
    a `wassup` command run alongside the app would otherwise deadlock with it. When it does run,
    one process at a time, and a lock conflict is waited out and retried."""
    import hashlib
    import time

    sql = (REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    digest = hashlib.sha256(sql.encode()).hexdigest()
    # Extensions must exist before register_vector runs, so use a plain connection here.
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        if not force and _applied(conn) == digest:
            return
        conn.execute("SELECT pg_advisory_lock(727001)")
        try:
            if not force and _applied(conn) == digest:
                return  # another process just applied it
            for attempt in range(6):
                try:
                    conn.execute("SET lock_timeout = '30s'")
                    conn.execute(sql)
                    sync_country_places(conn)
                    break
                except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable):
                    if attempt == 5:
                        raise
                    time.sleep(2 + 3 * attempt)
            conn.execute("RESET lock_timeout")
            conn.execute("""INSERT INTO kv (key, value, updated_at) VALUES ('schema.hash', %s, now())
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                         (psycopg.types.json.Jsonb(digest),))
        finally:
            conn.execute("SELECT pg_advisory_unlock(727001)")


def _applied(conn: psycopg.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = 'schema.hash'").fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    return row[0] if row else None


def sync_country_places(conn: psycopg.Connection) -> None:
    """Move country places to the gazetteer's current coordinates (they used to sit on the
    capital), along with any story whose main location is a country."""
    from .geo import gazetteer

    with conn.cursor() as cur:
        cur.executemany("UPDATE places SET lat = %s, lon = %s WHERE key = %s AND (lat <> %s OR lon <> %s)",
                        [(p.lat, p.lon, p.key, p.lat, p.lon) for p in gazetteer().countries.values()])
        cur.execute("""UPDATE stories s SET lat = p.lat, lon = p.lon FROM places p
                       WHERE s.primary_place_id = p.id AND p.key LIKE 'cc:%%' AND (s.lat <> p.lat OR s.lon <> p.lon)""")


def lock_stories(conn: psycopg.Connection, ids) -> None:
    """Lock story rows in id order before changing them. The pipeline lanes all change stories at
    once; taking locks in the same order makes them queue instead of deadlocking."""
    ids = sorted({int(i) for i in ids})
    if ids:
        conn.execute("SELECT id FROM stories WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE", (ids,))


def kv_get(conn: psycopg.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: psycopg.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, psycopg.types.json.Jsonb(value)),
    )
