"""Migration runner, user_id ownership, and the no-DDL-in-server contract."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from engineer_logbook import db_pg, db_spec, schema_version  # noqa: E402
from engineer_logbook.migrate import MigrationError, migrate  # noqa: E402

from conftest import migrate_fresh, reset_db  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)

ALL = ["0001_baseline", "0002_spec_workflows", "0003_user_id"]
SRC = Path(__file__).resolve().parents[1] / "src" / "engineer_logbook"


@pytest.fixture()
def empty():
    """A connection to an EMPTY scratch DB; restored to fully migrated after."""
    conn = db_pg.connect(DSN)
    reset_db(conn)
    try:
        yield conn
    finally:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        migrate_fresh(conn)
        conn.close()


def _exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS x", (name,)).fetchone()["x"]


def _sql(version_name: str) -> str:
    return next(
        m.sql for m in schema_version.available() if f"{m.version}_{m.name}" == version_name
    )


# ---------------------------------------------------------------- runner


def test_fresh_database_gets_every_migration_in_order(empty):
    out = migrate(empty)
    assert out["applied"] == ALL
    assert out["status"]["up_to_date"] is True
    assert out["status"]["current"] == "0003"
    for t in ("sessions", "search_index", "spec_workflows", "schema_migrations"):
        assert _exists(empty, t)


def test_rerun_applies_nothing(empty):
    migrate(empty)
    assert migrate(empty)["applied"] == []


def test_dry_run_changes_nothing(empty):
    out = migrate(empty, dry_run=True)
    assert out["would_apply"] == ALL
    assert not _exists(empty, "sessions")
    assert schema_version.status(empty)["pending"] == ALL


def test_edited_applied_migration_is_refused(empty):
    migrate(empty)
    empty.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = '0001'")
    empty.commit()
    assert schema_version.status(empty)["drifted"] == ["0001_baseline"]
    with pytest.raises(MigrationError, match="checksum mismatch"):
        migrate(empty)


def test_concurrent_runners_apply_each_migration_once(empty):
    results: list[dict] = []
    errors: list[BaseException] = []

    def run():
        c = db_pg.connect(DSN)
        try:
            results.append(migrate(c))
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    applied = sorted(m for r in results for m in r["applied"])
    assert applied == ALL
    count = empty.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    assert count == 3


# ---------------------------------------------------------------- live upgrade rehearsal


def test_upgrade_from_pre_migration_database_keeps_data(empty):
    """The live DB: old schema with data, no schema_migrations table."""
    empty.execute(_sql("0001_baseline"))
    sid = empty.execute(
        "INSERT INTO sessions(title, project, started_at) VALUES ('old', 'p', '2026-10-01T00:00:00+00:00')"
        " RETURNING id"
    ).fetchone()["id"]
    empty.execute(
        "INSERT INTO logs(session_id, timestamp, project, category, message, tags)"
        " VALUES (%s, '2026-10-01T00:00:00+00:00', 'p', 'info', 'quokka migration probe', '')",
        (sid,),
    )
    empty.execute(
        "INSERT INTO notes(title, content, created_at, updated_at)"
        " VALUES ('n', 'body', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')"
    )
    empty.execute("INSERT INTO paths(path) VALUES ('/repo')")
    empty.execute(
        "INSERT INTO chat_bindings(chat_id, session_id, bound_at)"
        " VALUES ('c1', %s, '2026-10-01T00:00:00+00:00')",
        (sid,),
    )
    empty.commit()
    index_before = empty.execute("SELECT COUNT(*) AS c FROM search_index").fetchone()["c"]
    assert index_before == 2  # log + note

    out = migrate(empty)
    assert out["applied"] == ALL  # 0001 re-applies harmlessly over existing objects

    for t in ("sessions", "logs", "notes", "paths", "chat_bindings", "search_index"):
        owners = {r["user_id"] for r in empty.execute(f"SELECT user_id FROM {t}").fetchall()}
        assert owners == {"chris"}, t
    index_after = empty.execute("SELECT COUNT(*) AS c FROM search_index").fetchone()["c"]
    assert index_after == index_before
    assert db_pg.search(empty, "quokka")["count"] == 1


# ---------------------------------------------------------------- ownership


def test_new_rows_take_the_connection_user(empty):
    migrate(empty)
    db_pg.set_user(empty, "alice")
    n = db_pg.add_note(empty, "alice note", "wombat body")["note"]
    empty.commit()
    assert n["user_id"] == "alice"
    row = empty.execute(
        "SELECT user_id FROM search_index WHERE entry_type = 'note' AND ref_id = %s", (n["id"],)
    ).fetchone()
    assert row["user_id"] == "alice"


def test_search_rows_keep_the_source_owner_not_the_writer(empty):
    migrate(empty)
    db_pg.set_user(empty, "alice")
    n = db_pg.add_note(empty, "owned", "body")["note"]
    empty.commit()
    db_pg.set_user(empty, "bob")  # a different connection user edits it
    db_pg.update_note(empty, n["id"], content="edited")
    empty.commit()
    row = empty.execute(
        "SELECT user_id FROM search_index WHERE entry_type = 'note' AND ref_id = %s", (n["id"],)
    ).fetchone()
    assert row["user_id"] == "alice"


def test_insert_without_a_user_is_rejected(empty):
    migrate(empty)
    raw = psycopg.connect(DSN)  # bypasses db_pg.connect, so no logbook.user_id
    try:
        with pytest.raises(psycopg.errors.NotNullViolation):
            raw.execute(
                "INSERT INTO notes(title, content, created_at, updated_at)"
                " VALUES ('x', 'y', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')"
            )
    finally:
        raw.rollback()
        raw.close()


def test_paths_are_unique_per_user(empty):
    migrate(empty)
    for user in ("alice", "bob"):
        db_pg.set_user(empty, user)
        empty.execute("INSERT INTO paths(path) VALUES ('/shared')")
        empty.commit()
    db_pg.set_user(empty, "alice")
    with pytest.raises(psycopg.errors.UniqueViolation):
        empty.execute("INSERT INTO paths(path) VALUES ('/shared')")
    empty.rollback()


def test_one_active_spec_workflow_per_user_and_project(empty):
    """The DB constraint: one active workflow per (user, project)."""
    migrate(empty)
    insert = (
        "INSERT INTO spec_workflows(process, project, created_at, updated_at)"
        " VALUES ('sdd', 'shared', 'x', 'x')"
    )
    for user in ("alice", "bob"):
        db_pg.set_user(empty, user)
        empty.execute(insert)  # different users: both allowed
        empty.commit()
    db_pg.set_user(empty, "alice")
    with pytest.raises(psycopg.errors.UniqueViolation):
        empty.execute(insert)
    empty.rollback()


@pytest.mark.xfail(
    strict=True,
    reason="queries are not user-scoped yet: bob's spec_start replaces alice's "
    "workflow. Fixed by the row-level-security follow-up; remove this xfail then.",
)
def test_spec_start_is_isolated_between_users(empty):
    migrate(empty)
    db_pg.set_user(empty, "alice")
    db_spec.spec_start(empty, "sdd", project="shared")
    empty.commit()
    db_pg.set_user(empty, "bob")
    db_spec.spec_start(empty, "sdd", project="shared")
    empty.commit()
    rows = empty.execute(
        "SELECT user_id FROM spec_workflows WHERE status = 'active' ORDER BY user_id"
    ).fetchall()
    assert [r["user_id"] for r in rows] == ["alice", "bob"]


# ---------------------------------------------------------------- server contract


def test_require_current_names_pending_migrations(empty):
    migrate(empty)
    empty.execute("DELETE FROM schema_migrations WHERE version = '0003'")
    empty.commit()
    with pytest.raises(schema_version.SchemaOutOfDate, match="0003_user_id"):
        schema_version.require_current(empty)


def test_server_refuses_to_start_on_stale_schema(empty):
    empty.execute(_sql("0001_baseline"))  # old schema, never migrated
    empty.commit()
    proc = subprocess.run(
        [sys.executable, "-m", "engineer_logbook.server"],
        env={**os.environ, "ENGINEER_LOGBOOK_PG_DSN": DSN},
        input="", capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "engineer-logbook-migrate up" in proc.stderr
    assert proc.stdout == ""  # stdout is the MCP channel; nothing leaks onto it


def test_server_code_contains_no_ddl():
    """Schema changes live only in migrations/ and the runner."""
    ddl = re.compile(
        r"\b(DROP\s+(TABLE|INDEX|TRIGGER|FUNCTION|VIEW|SCHEMA|CONSTRAINT)"
        r"|ALTER\s+TABLE|CREATE\s+(OR\s+REPLACE\s+)?(TABLE|INDEX|UNIQUE|TRIGGER|FUNCTION|VIEW))\b"
    )
    for name in ("server.py", "db.py", "db_pg.py", "db_spec.py", "db_common.py",
                 "schema_version.py", "spec_render.py", "spec_manifest.py"):
        hits = ddl.findall((SRC / name).read_text())
        assert not hits, f"{name} contains DDL: {hits}"
