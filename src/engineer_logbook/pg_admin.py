"""Admin CLI for the Postgres backend of the engineer logbook.

Commands (run with the target DSN in the environment — POSTGRES_* vars,
ENGINEER_LOGBOOK_PG_DSN, or --dsn):

    python -m engineer_logbook.pg_admin init            # create schema
    python -m engineer_logbook.pg_admin migrate \\      # copy SQLite -> PG
        --sqlite ~/.local/share/engineer-logbook/logbook.db
    python -m engineer_logbook.pg_admin reindex         # rebuild search vectors
    python -m engineer_logbook.pg_admin verify           # print table counts

init/reindex/verify can be run against an empty or populated database; all
are idempotent. migrate inserts rows with their original ids (preserving
session_id / ref_id references) and re-syncs identity sequences.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from engineer_logbook import db_pg

TABLES: dict[str, tuple[str, ...]] = {
    "sessions": ("id", "title", "project", "started_at", "ended_at", "summary",
                 "context", "chat_id", "last_activity_at", "closed_reason"),
    "logs": ("id", "session_id", "timestamp", "project", "category", "message", "tags"),
    "notes": ("id", "title", "content", "priority", "pinned", "project", "tags",
              "created_at", "updated_at"),
    "paths": ("id", "path", "kind", "description", "project", "last_referenced"),
    "tasks": ("id", "description", "status", "due_date", "project", "created_at",
              "completed_at"),
    "decisions": ("id", "title", "decision", "rationale", "alternatives", "project",
                  "created_at"),
    "snippets": ("id", "language", "code", "description", "project", "tags", "created_at"),
    "chat_bindings": ("chat_id", "session_id", "bound_at"),
}


def cmd_init(args) -> int:
    """Alias for `engineer-logbook-migrate up` (schema = versioned migrations)."""
    from engineer_logbook.migrate import MigrationError, migrate

    with db_pg.connect(args.dsn) as conn:
        try:
            out = migrate(conn)
        except MigrationError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    print(json.dumps({"ok": True, **out}))
    return 0


def _sqlite_rows(sqlite_path: Path, table: str, cols: tuple[str, ...]):
    sq = sqlite3.connect(str(sqlite_path))
    sq.row_factory = sqlite3.Row
    try:
        cur = sq.execute(f"SELECT {', '.join(cols)} FROM {table}")
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                return
            yield [tuple(r) for r in batch]
    finally:
        sq.close()


def cmd_migrate(args) -> int:
    sqlite_path = Path(args.sqlite).expanduser()
    if not sqlite_path.exists():
        print(f"error: SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 1
    sq = sqlite3.connect(str(sqlite_path))
    sq.row_factory = sqlite3.Row
    counts = {t: sq.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    sq.close()
    print(f"source: {sqlite_path}  rows: {counts}")

    with db_pg.connect(args.dsn) as conn:
        with conn.transaction():
            for table, cols in TABLES.items():
                total = 0
                placeholders = ", ".join(["%s"] * len(cols))
                col_list = ", ".join(cols)
                for batch in _sqlite_rows(sqlite_path, table, cols):
                    conn.cursor().executemany(
                        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
                        " ON CONFLICT DO NOTHING",
                        batch,
                    )
                    total += len(batch)
                print(f"  {table}: {total} rows")
            # re-sync identity sequences past imported ids
            for table in TABLES:
                if table == "chat_bindings" or "id" not in TABLES[table]:
                    continue
                conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                    f" COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
        # rebuild search vectors from the imported rows
        _reindex(conn)
    print("migrated. verify with: python -m engineer_logbook.pg_admin verify")
    return 0


_SI_COLS = "entry_type, ref_id, title, content, tags, project, tsv, user_id"


def _reindex(conn) -> None:
    """Rebuild search_index from the source tables (rows keep their owner)."""
    with conn.transaction():
        conn.execute("DELETE FROM search_index")
        conn.execute(
            f"INSERT INTO search_index({_SI_COLS})"
            " SELECT 'log', id, category, message, tags, project,"
            " setweight(to_tsvector('simple', category), 'A')"
            " || setweight(to_tsvector('simple', message), 'B')"
            " || setweight(to_tsvector('simple', tags), 'C'), user_id FROM logs"
        )
        conn.execute(
            f"INSERT INTO search_index({_SI_COLS})"
            " SELECT 'note', id, title, content, tags, project,"
            " setweight(to_tsvector('simple', title), 'A')"
            " || setweight(to_tsvector('simple', content), 'B')"
            " || setweight(to_tsvector('simple', tags), 'C'), user_id FROM notes"
        )
        conn.execute(
            f"INSERT INTO search_index({_SI_COLS})"
            " SELECT 'decision', id, title,"
            " decision || ' ' || COALESCE(rationale, '') || ' ' || COALESCE(alternatives, ''),"
            " '', project,"
            " setweight(to_tsvector('simple', title), 'A')"
            " || setweight(to_tsvector('simple', decision || ' ' || COALESCE(rationale, '') || ' ' || COALESCE(alternatives, '')), 'B'),"
            " user_id FROM decisions"
        )
        conn.execute(
            f"INSERT INTO search_index({_SI_COLS})"
            " SELECT 'snippet', id, COALESCE(description, language), code, tags, project,"
            " setweight(to_tsvector('simple', COALESCE(description, language)), 'A')"
            " || setweight(to_tsvector('simple', code), 'B')"
            " || setweight(to_tsvector('simple', tags), 'C'), user_id FROM snippets"
        )
        conn.execute(
            f"INSERT INTO search_index({_SI_COLS})"
            " SELECT 'spec_artifact', a.id, a.step || ': ' || a.path, COALESCE(a.content, ''),"
            " 'spec-artifact', w.project,"
            " setweight(to_tsvector('simple', a.step), 'A')"
            " || setweight(to_tsvector('simple', a.path), 'B')"
            " || setweight(to_tsvector('simple', COALESCE(a.content, '')), 'C'), a.user_id"
            " FROM spec_artifacts a JOIN spec_workflows w ON w.id = a.workflow_id"
        )


def cmd_reindex(args) -> int:
    with db_pg.connect(args.dsn) as conn:
        _reindex(conn)
    print(json.dumps({"reindexed": True}))
    return 0


def cmd_verify(args) -> int:
    with db_pg.connect(args.dsn) as conn:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in list(TABLES) + ["search_index"]
        }
        # a couple of sanity probes
        probes = {}
        probes["active_sessions"] = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE ended_at IS NULL"
        ).fetchone()["c"]
        probes["search_sample"] = conn.execute(
            "SELECT COUNT(*) AS c FROM search_index LIMIT 1"
        ).fetchone()["c"]
    print(json.dumps({"counts": counts, "probes": probes}, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="pg_admin", description=__doc__)
    ap.add_argument("--dsn", help="Postgres DSN (defaults to env: POSTGRES_* / ENGINEER_LOGBOOK_PG_DSN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create tables, triggers, indexes (idempotent)")

    p_mig = sub.add_parser("migrate", help="copy SQLite data into Postgres")
    p_mig.add_argument("--sqlite", default=str(
        Path.home() / ".local" / "share" / "engineer-logbook" / "logbook.db"
    ), help="path to the SQLite logbook to import")

    sub.add_parser("reindex", help="rebuild the search_index tsvector rows")
    sub.add_parser("verify", help="print table row counts")

    args = ap.parse_args()
    handlers = {"init": cmd_init, "migrate": cmd_migrate,
                "reindex": cmd_reindex, "verify": cmd_verify}
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()