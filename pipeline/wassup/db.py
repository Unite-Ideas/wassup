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
        _pool = ConnectionPool(settings().database_url, min_size=1, max_size=10, configure=_configure,
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


def kv_get(conn: psycopg.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: psycopg.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, psycopg.types.json.Jsonb(value)),
    )
