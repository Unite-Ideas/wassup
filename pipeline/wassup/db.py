"""Postgres access. One connection pool per process."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
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


def init_schema() -> None:
    sql = (REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    # Extensions must exist before register_vector runs, so use a plain connection here.
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        conn.execute(sql)
        sync_country_places(conn)


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
