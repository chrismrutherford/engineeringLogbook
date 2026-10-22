"""Migration runner: the only code that changes the database schema.

    engineer-logbook-migrate status          # what's applied / pending / drifted
    engineer-logbook-migrate up --dry-run    # show what would run
    engineer-logbook-migrate up              # apply pending migrations

Connection comes from --dsn, ENGINEER_LOGBOOK_PG_DSN, or POSTGRES_* (same as
the server). Each migration runs in its own transaction and is recorded in
schema_migrations with a checksum; a session-level advisory lock serialises
concurrent runners (e.g. several replicas deploying at once). Applied
migrations must never be edited: a checksum mismatch stops the run.

For a remote deployment, run `up` as a deploy step before starting the
server; the server itself only verifies the version (schema_version.py).
"""

from __future__ import annotations

import argparse
import json
import sys

import psycopg

from engineer_logbook import db_pg, schema_version
from engineer_logbook.db_common import utcnow

# arbitrary constant: pg_advisory_lock key reserved for logbook migrations
LOCK_KEY = 0x10_6B_B0_0C

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class MigrationError(RuntimeError):
    """A migration run was refused or failed."""


def migrate(conn: psycopg.Connection, dry_run: bool = False) -> dict:
    """Apply pending migrations in order. Returns what was (or would be) applied."""
    was_autocommit = conn.autocommit
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.commit()
    conn.autocommit = True
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        with conn.transaction():
            conn.execute(_BOOTSTRAP)
        st = schema_version.status(conn)
        if st["drifted"]:
            raise MigrationError(
                "applied migrations were edited after being applied (checksum mismatch): "
                f"{', '.join(st['drifted'])}. Add a new migration instead."
            )
        todo = [m for m in schema_version.available()
                if f"{m.version}_{m.name}" in st["pending"]]
        if dry_run:
            return {"dry_run": True, "would_apply": [f"{m.version}_{m.name}" for m in todo],
                    "status": st}
        applied: list[str] = []
        for m in todo:
            try:
                with conn.transaction():
                    conn.execute(m.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, applied_at)"
                        " VALUES (%s, %s, %s, %s)",
                        (m.version, m.name, m.checksum, utcnow()),
                    )
            except psycopg.Error as e:
                raise MigrationError(
                    f"migration {m.version}_{m.name} failed and was rolled back: {e}"
                    + (f" (already applied this run: {', '.join(applied)})" if applied else "")
                ) from e
            applied.append(f"{m.version}_{m.name}")
        return {"dry_run": False, "applied": applied, "status": schema_version.status(conn)}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.autocommit = was_autocommit


def main() -> None:
    ap = argparse.ArgumentParser(prog="engineer-logbook-migrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="Postgres DSN (default: env, same as the server)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show applied / pending / drifted migrations")
    p_up = sub.add_parser("up", help="apply pending migrations")
    p_up.add_argument("--dry-run", action="store_true", help="show what would run")
    args = ap.parse_args()

    try:
        with db_pg.connect(args.dsn) as conn:
            if args.cmd == "status":
                out = schema_version.status(conn)
            else:
                out = migrate(conn, dry_run=args.dry_run)
    except MigrationError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        sys.exit(1)
    print(json.dumps({"ok": True, **out}, indent=2))


if __name__ == "__main__":
    main()
