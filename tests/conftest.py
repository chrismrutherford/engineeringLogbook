"""Shared test setup: build the scratch database from migrations, once per run.

The scratch database (ENGINEER_LOGBOOK_PG_TEST_DSN, e.g. logbookdb_test) is
reset to empty and migrated with the real migration runner, so every test run
exercises the fresh-install path. Individual modules still wipe rows between
tests. Never point ENGINEER_LOGBOOK_PG_TEST_DSN at a database you care about.
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

# Fail fast instead of hanging: a blocked lock or runaway statement errors out
# (libpq reads PGOPTIONS; server subprocesses inherit it).
os.environ.setdefault("PGOPTIONS", "-c lock_timeout=5s -c statement_timeout=15s")

# Every object the migrations create (test-only teardown; the server and the
# migrations never drop these).
_TABLES = (
    "spec_events", "spec_artifacts", "spec_workflows", "chat_bindings", "logs",
    "notes", "paths", "tasks", "decisions", "snippets", "sessions",
    "search_index", "schema_migrations",
)
_FUNCTIONS = (
    "logbook_tsv", "logs_tsv", "logs_tsv_del", "notes_tsv", "notes_tsv_del",
    "decisions_tsv", "decisions_tsv_del", "snippets_tsv", "snippets_tsv_del",
    "spec_artifacts_tsv", "spec_artifacts_tsv_del",
)


_DATA_TABLES = (
    "chat_bindings", "spec_events", "spec_artifacts", "spec_workflows", "logs",
    "notes", "paths", "tasks", "decisions", "snippets", "sessions", "search_index",
)


def wipe(conn) -> None:
    """Empty every data table and restart ids, in one round trip."""
    conn.execute(f"TRUNCATE {', '.join(_DATA_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()


def reset_db(conn) -> None:
    """Drop every logbook object from the scratch database."""
    conn.execute("DROP VIEW IF EXISTS spec_artifacts_view CASCADE")
    for t in _TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    for row in conn.execute(
        "SELECT p.oid::regprocedure::text AS sig FROM pg_proc p"
        " WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(%s)",
        (list(_FUNCTIONS),),
    ).fetchall():
        conn.execute(f"DROP FUNCTION IF EXISTS {row['sig']} CASCADE")
    conn.commit()


def migrate_fresh(conn) -> dict:
    from engineer_logbook.migrate import migrate

    reset_db(conn)
    return migrate(conn)


@pytest.fixture(scope="session", autouse=True)
def _migrated_scratch_db():
    if not DSN:
        yield
        return
    pytest.importorskip("psycopg")
    from engineer_logbook import db_pg

    conn = db_pg.connect(DSN)
    try:
        migrate_fresh(conn)
    finally:
        conn.close()
    yield
