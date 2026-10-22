"""Integration tests for Postgres-specific behaviour: triggers/reindex,
sequence resync via pg_admin, and note-update search refresh."""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from engineer_logbook import db_pg, pg_admin  # noqa: E402

from conftest import wipe  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


@pytest.fixture()
def conn():
    c = db_pg.connect(DSN)
    wipe(c)
    c.commit()
    yield c
    c.close()


def test_note_update_refreshes_search(conn):
    n = db_pg.add_note(conn, "Original title", "original body")["note"]
    assert db_pg.search(conn, "original")["count"] == 1
    db_pg.update_note(conn, n["id"], title="Renamed", content="new body")
    assert db_pg.search(conn, "original")["count"] == 0
    assert db_pg.search(conn, "renamed")["count"] == 1
    db_pg.delete_note(conn, n["id"])
    assert db_pg.search(conn, "renamed")["count"] == 0


def test_search_and_semantics(conn):
    db_pg.add_log(conn, "docker networking bug", tags=["docker"])
    db_pg.add_note(conn, "Kafka notes", "consumer rebalancing")
    db_pg.add_snippet(conn, "docker compose up -d", language="bash", description="start stack")

    # AND semantics: both terms must match
    res = db_pg.search(conn, "docker networking")
    assert {r["type"] for r in res["results"]} == {"log"}


def test_reindex_rebuilds_from_rows(conn):
    db_pg.add_log(conn, "redisable me", category="info")
    db_pg.add_note(conn, "Reindexed note", "body")
    # wipe the index, rebuild purely from table rows
    conn.execute("DELETE FROM search_index")
    conn.commit()
    assert db_pg.search(conn, "redisable")["count"] == 0

    class A:
        dsn = DSN
    assert pg_admin.cmd_reindex(A()) == 0
    c2 = db_pg.connect(DSN)  # reindex used its own connection
    assert db_pg.search(c2, "redisable")["count"] == 1
    assert db_pg.search(c2, "reindexed")["count"] == 1
    c2.close()


def test_verify_reports_counts(conn):
    db_pg.add_note(conn, "counted", "body")
    class A:
        dsn = DSN
    assert pg_admin.cmd_verify(A()) == 0


def test_sequence_resync_on_duplicate_import(conn):
    """pg_admin-style insert with explicit ids must push the identity
    sequence past the imported ids (what migrate does after copying)."""
    conn.execute(
        "INSERT INTO logs(id, session_id, timestamp, project, category, message, tags)"
        " VALUES (5, NULL, '2026-10-05T00:00:00+00:00', 'default', 'info', 'imported', '')"
    )
    conn.execute(
        "SELECT setval(pg_get_serial_sequence('logs', 'id'),"
        " COALESCE((SELECT MAX(id) FROM logs), 0) + 1, false)"
    )
    conn.commit()
    new_id = conn.execute(
        "INSERT INTO logs(session_id, timestamp, project, category, message, tags)"
        " VALUES (NULL, '2026-10-05T00:00:00+00:00', 'default', 'info', 'fresh row', '')"
        " RETURNING id"
    ).fetchone()["id"]
    assert new_id == 6