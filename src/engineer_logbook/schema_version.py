"""Read-only view of the database schema version.

Migrations are numbered SQL files in engineer_logbook/migrations
(NNNN_name.sql). The applied ones are recorded in schema_migrations by the
migration runner (engineer_logbook.migrate). This module only READS: the
MCP server uses it at startup to refuse to run against a database that is
behind the code. It never executes DDL.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

import psycopg

MIGRATIONS_PACKAGE = "engineer_logbook.migrations"
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class SchemaOutOfDate(RuntimeError):
    """The database is missing migrations the code depends on."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def available() -> list[Migration]:
    """All bundled migrations in version order."""
    out: list[Migration] = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        m = _FILE_RE.match(entry.name)
        if not m:
            continue
        sql = entry.read_text(encoding="utf-8")
        out.append(
            Migration(
                version=m.group(1),
                name=m.group(2),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    out.sort(key=lambda mig: mig.version)
    versions = [mig.version for mig in out]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions in {MIGRATIONS_PACKAGE}: {versions}")
    return out


def latest_version() -> str | None:
    migs = available()
    return migs[-1].version if migs else None


def applied(conn: psycopg.Connection) -> dict[str, str]:
    """version -> checksum for applied migrations ({} if never migrated)."""
    exists = conn.execute(
        "SELECT to_regclass('schema_migrations') IS NOT NULL AS present"
    ).fetchone()
    present = exists["present"] if isinstance(exists, dict) else exists[0]
    if not present:
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {
        (r["version"] if isinstance(r, dict) else r[0]): (r["checksum"] if isinstance(r, dict) else r[1])
        for r in rows
    }


def status(conn: psycopg.Connection) -> dict:
    """Current vs latest version, pending migrations, and checksum drift."""
    migs = available()
    done = applied(conn)
    known = {m.version for m in migs}
    pending = [f"{m.version}_{m.name}" for m in migs if m.version not in done]
    drifted = [
        f"{m.version}_{m.name}" for m in migs
        if m.version in done and done[m.version] != m.checksum
    ]
    unknown = sorted(v for v in done if v not in known)
    return {
        "current": max(done) if done else None,
        "latest": migs[-1].version if migs else None,
        "pending": pending,
        "drifted": drifted,
        "unknown": unknown,
        "up_to_date": not pending,
    }


def require_current(conn: psycopg.Connection) -> dict:
    """Raise SchemaOutOfDate if any bundled migration is unapplied."""
    st = status(conn)
    if st["pending"]:
        raise SchemaOutOfDate(
            f"database schema is at {st['current'] or 'none'}, code needs {st['latest']}; "
            f"pending: {', '.join(st['pending'])}. Run: engineer-logbook-migrate up"
        )
    return st
