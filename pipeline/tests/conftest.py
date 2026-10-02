"""Test setup. Unit tests need nothing. Integration tests need a Postgres server with the
pgvector and PostGIS extensions; set WASSUP_TEST_ADMIN_URL to a superuser connection
(for example postgresql://postgres@localhost:5432/postgres) and a throwaway wassup_test
database is created for each run. Without it, integration tests are skipped."""
import os

import pytest

ADMIN_URL = os.environ.get("WASSUP_TEST_ADMIN_URL")
TEST_DB = "wassup_test"

os.environ["EMBED_BACKEND"] = "hash"
os.environ["TRIAGE_BACKEND"] = "rules"
if ADMIN_URL:
    base = ADMIN_URL.rsplit("/", 1)[0]
    os.environ["DATABASE_URL"] = f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def database():
    if not ADMIN_URL:
        pytest.skip("set WASSUP_TEST_ADMIN_URL to run integration tests")
    import psycopg

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    from wassup import db

    db.init_schema()
    yield os.environ["DATABASE_URL"]
    db.pool().close()
    db._pool = None
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
