"""PostgreSQL storage layer for the engineer logbook.

This is the storage API the MCP server and the TUI program against:
sessions, logs, notes, paths, tasks, decisions, snippets, search, reports.

Timestamps are ISO 8601 strings (TEXT in Postgres) so ordering stays
lexicographic and data round-trips exactly. Tags are comma-separated
strings.

Connection management: the MCP server makes several tool calls per chat
message; over TLS to a remote instance, opening a fresh connection per call
is slow, so a module-level psycopg_pool.ConnectionPool is used, checked out
per operation (pool()/connect(); direct connections for admin and tests).
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from engineer_logbook import db_common as _c

VALID_PRIORITIES = _c.VALID_PRIORITIES
VALID_TASK_STATUSES = _c.VALID_TASK_STATUSES
SEARCHABLE_TYPES = _c.SEARCHABLE_TYPES
DEFAULT_IDLE_MINUTES = _c.DEFAULT_IDLE_MINUTES

chat_id = _c.chat_id
utcnow = _c.utcnow

_HYDRATE_TABLE = {
    "log": "logs",
    "note": "notes",
    "decision": "decisions",
    "snippet": "snippets",
    "spec_artifact": "spec_artifacts_view",
}

# ---------------------------------------------------------------- DSN

_PG_ENV_MAP = {
    "POSTGRES_HOST": "host",
    "POSTGRES_PORT": "port",
    "POSTGRES_DB": "dbname",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "password",
}


def pg_dsn(
    dsn: str | None = None,
    env_prefix: str = "POSTGRES_",
) -> str | None:
    """Build a psycopg connection string from a DSN or POSTGRES_* env vars.

    Precedence: explicit dsn > ENGINEER_LOGBOOK_PG_DSN > POSTGRES_* vars.
    PGSSLMODE is honoured if set (DigitalOcean managed PG requires SSL).
    """
    if dsn:
        return dsn
    env = os.environ.get("ENGINEER_LOGBOOK_PG_DSN")
    if env:
        return env
    host = os.environ.get("POSTGRES_HOST")
    if not host:
        return None
    params: dict[str, str] = {"host": host}
    for env_name, kw in _PG_ENV_MAP.items():
        if kw == "host":
            continue
        val = os.environ.get(env_name)
        if val:
            params[kw] = val
    if not params.get("dbname"):
        params["dbname"] = "logbookdb"
    sslmode = os.environ.get("PGSSLMODE") or os.environ.get("POSTGRES_SSLMODE")
    if sslmode:
        params["sslmode"] = sslmode
    else:
        params["sslmode"] = "require"
    return " ".join(f"{k}={v}" for k, v in params.items())


_POOL: ConnectionPool | None = None


def _conn_kwargs(dsn: str) -> dict[str, Any]:
    """Extra connection params not already set by the DSN or environment.

    libpq's default gssencmode=prefer first attempts GSSAPI (Kerberos)
    encryption; on hosts with a Kerberos setup but no ticket for the server
    that costs ~4-5 s per connection before falling back to SSL. Managed PG
    (DigitalOcean) doesn't offer GSSAPI, so skip the probe unless the DSN or
    PGGSSENCMODE asks for it.
    """
    info = conninfo_to_dict(dsn)
    extra: dict[str, Any] = {}
    if "gssencmode" not in info and "PGGSSENCMODE" not in os.environ:
        extra["gssencmode"] = "disable"
    # Fail fast on an unreachable server, and use TCP keepalives so a peer
    # that vanishes (sleep, NAT drop) is detected within ~60 s instead of
    # leaving a zombie connection that blocks the calling terminal.
    if "connect_timeout" not in info:
        extra["connect_timeout"] = 5
    extra.setdefault("keepalives", 1)
    extra.setdefault("keepalives_idle", 30)
    extra.setdefault("keepalives_interval", 10)
    extra.setdefault("keepalives_count", 3)
    # Owner for user_id defaults, sent in the startup packet (no extra round
    # trip). Appended to any options from the DSN or PGOPTIONS.
    uid = _c.user_id().replace("\\", "\\\\").replace(" ", "\\ ")
    base = info.get("options") or os.environ.get("PGOPTIONS", "")
    extra["options"] = f"{base} -c logbook.user_id={uid}".strip()
    return extra


def set_user(conn: psycopg.Connection, user: str | None = None) -> None:
    """Switch the logbook.user_id session setting on an open connection.

    New connections already get it from the startup packet (_conn_kwargs);
    this is for changing owner mid-connection (tests; per-request identity
    later). Session-level and committed, so a later rollback can't revert it.
    """
    conn.execute("SELECT set_config('logbook.user_id', %s, false)", (user or _c.user_id(),))
    conn.commit()


def pool(dsn: str | None = None) -> ConnectionPool:
    """Module-level connection pool (created lazily, one per process).

    Resilience settings so dead connections never block OC terminals:

    - keepalives on every connection: the server notices dropped TCP peers
      (laptop sleep, NAT expiry) within ~60 s instead of hanging forever.
    - connect_timeout=5: a down/unreachable server fails fast.
    - check=_conn_ok on checkout: connections that broke while idle in the
      pool are discarded and a fresh one is opened (reconnect), so the
      caller sees at most one retry, never a stale-connection error loop.
    - timeout=10: waiting for a free pooled connection gives up after 10 s
      instead of queueing indefinitely (PoolTimeout, actionable).
    - max_idle/max_lifetime: connections recycles before server-side or
      load-balancer idle limits kill them mid-operation.
    """
    global _POOL
    if _POOL is None:
        dsn_ = pg_dsn(dsn)
        if not dsn_:
            raise RuntimeError(
                "No Postgres DSN configured. Set ENGINEER_LOGBOOK_PG_DSN or "
                "POSTGRES_HOST/POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB."
            )
        _POOL = ConnectionPool(
            dsn_,
            min_size=1,
            max_size=8,
            timeout=10,
            max_idle=300,
            max_lifetime=1800,
            reconnect_timeout=15,
            check=_conn_ok,
            kwargs={"row_factory": dict_row, "autocommit": False, **_conn_kwargs(dsn_)},
            open=True,
        )
    return _POOL


def _conn_ok(conn: psycopg.Connection) -> bool:
    """Pool checkout check: discard connections that broke while idle."""
    try:
        conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        logging.getLogger("engineer_logbook.db").warning(
            "discarding broken pooled Postgres connection"
        )
        try:
            conn.close()
        except psycopg.Error:
            pass
        return False


def connect(dsn: str | None = None) -> psycopg.Connection:
    """A direct (non-pooled) connection — used by admin tools and tests."""
    dsn_ = pg_dsn(dsn)
    if not dsn_:
        raise RuntimeError(
            "No Postgres DSN configured. Set ENGINEER_LOGBOOK_PG_DSN or "
            "POSTGRES_HOST/POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB."
        )
    return psycopg.connect(dsn_, row_factory=dict_row, **_conn_kwargs(dsn_))


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


def describe() -> str:
    """Human-readable connection target for UI subtitles (password masked)."""
    dsn_ = pg_dsn()
    if not dsn_:
        return "postgres: no DSN configured"
    info = conninfo_to_dict(dsn_)
    host = info.get("host", "?")
    port = info.get("port", 5432)
    dbname = info.get("dbname", "?")
    return f"postgres://{host}:{port}/{dbname}"


# ---------------------------------------------------------------- schema
#
# The schema lives in versioned migrations (engineer_logbook/migrations/*.sql)
# applied by engineer_logbook.migrate. This module never executes DDL; the
# server checks the version at startup via engineer_logbook.schema_version.


# ---------------------------------------------------------------- rows

def _d(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


def _ds(rows: list[dict]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- sessions


def get_active_session(conn: psycopg.Connection, chat: str | None = None) -> dict | None:
    """Active session for a chat (or the globally-active one when unscoped)."""
    if chat is None:
        row = conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT s.* FROM sessions s"
            " JOIN chat_bindings b ON b.session_id = s.id AND b.chat_id = %s"
            " WHERE s.ended_at IS NULL LIMIT 1",
            (chat,),
        ).fetchone()
    return _d(row)


def _bind(conn: psycopg.Connection, chat: str, session_id: int) -> None:
    conn.execute(
        "INSERT INTO chat_bindings(chat_id, session_id, bound_at) VALUES (%s, %s, %s)"
        " ON CONFLICT(chat_id) DO UPDATE SET session_id = excluded.session_id,"
        " bound_at = excluded.bound_at",
        (chat, session_id, utcnow()),
    )


def _unbind(conn: psycopg.Connection, chat: str) -> None:
    conn.execute("DELETE FROM chat_bindings WHERE chat_id = %s", (chat,))


def _prior_context(
    conn: psycopg.Connection, project: str, title: str, session_id: int, limit: int = 5
) -> dict:
    """What an agent should review before starting fresh work.

    Related sessions: same project, plus title-keyword matches anywhere.
    Notes: pinned/high-priority for the project (always), plus keyword hits.
    Plus open tasks for the project. Everything best-effort — never raises.
    """
    project = project or "default"
    tokens = _c._tokens(title)

    def _recent_sessions() -> list[dict]:
        rows = _ds(
            conn.execute(
                "SELECT id, title, project, started_at, ended_at, summary,"
                " closed_reason FROM sessions WHERE project = %s"
                " ORDER BY started_at DESC LIMIT %s",
                (project, limit + 1),
            ).fetchall()
        )
        for s in rows:
            s["active"] = s["ended_at"] is None
        return rows

    def _related_sessions() -> list[dict]:
        if not tokens:
            return []
        out: list[dict] = []
        seen: set[int] = set()
        for tok in tokens:
            like = f"%{tok}%"
            for row in conn.execute(
                "SELECT id, title, project, started_at, ended_at, summary, closed_reason"
                " FROM sessions WHERE (title ILIKE %s OR COALESCE(summary, '') ILIKE %s)"
                " ORDER BY started_at DESC LIMIT %s",
                (like, like, limit),
            ).fetchall():
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                out.append(_d(row))
        for s in out:
            s["active"] = s["ended_at"] is None
        return out[:limit]

    def _relevant_notes() -> list[dict]:
        notes = _ds(
            conn.execute(
                "SELECT id, title, priority, tags FROM notes WHERE project = %s"
                " AND (pinned = 1 OR priority = 'high')"
                " ORDER BY pinned DESC, updated_at DESC LIMIT %s",
                (project, limit),
            ).fetchall()
        )
        if not tokens:
            return notes
        seen = {n["id"] for n in notes}
        for tok in tokens:
            like = f"%{tok}%"
            for row in conn.execute(
                "SELECT id, title, priority, project, tags FROM notes"
                " WHERE (title ILIKE %s OR content ILIKE %s)"
                " ORDER BY updated_at DESC LIMIT %s",
                (like, like, limit),
            ).fetchall():
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                notes.append(_d(row))
        return notes[: max(limit * 2, 10)]

    def _open_tasks() -> list[dict]:
        return _ds(
            conn.execute(
                "SELECT id, description, due_date, project FROM tasks"
                " WHERE status = 'open' AND project = %s"
                " ORDER BY created_at DESC LIMIT %s",
                (project, limit),
            ).fetchall()
        )

    try:
        # savepoint: a failure here must not roll back the new session
        with conn.transaction():
            out = {
                "recent_sessions_same_project": _recent_sessions(),
                "related_sessions_by_title": _related_sessions(),
                "notes_pinned_or_priority": _relevant_notes(),
                "open_tasks": _open_tasks(),
            }
            # don't point the agent at the session it just created
            for key in ("recent_sessions_same_project", "related_sessions_by_title"):
                out[key] = [s for s in out[key] if s["id"] != session_id][:limit]
            workflow_nag = _workflow_nag(conn, project or "default")
            if workflow_nag:
                out["spec_workflow"] = workflow_nag
            out["hint"] = (
                "Review these before proceeding; if a recent/related session matches "
                "this work, consider resume_session(id) instead of duplicating it. "
                "Call restore_session_context(session_id=...) to pull its saved context."
            )
            return out
    except Exception as e:  # prior context is best-effort
        return {"error": f"prior context unavailable: {e}"}


def _workflow_nag(conn: psycopg.Connection, project: str) -> str | None:
    """Best-effort active-workflow nag for session-start injection.

    Runs inside a savepoint: a failing query must not abort the caller's
    transaction (Postgres would then turn the final COMMIT into a silent
    ROLLBACK of everything the tool call wrote).
    """
    try:
        from . import db_spec

        with conn.transaction():
            return db_spec.active_workflow_nag(conn, project)
    except Exception:  # best-effort; the savepoint already rolled back
        return None


def start_session(
    conn: psycopg.Connection, title: str, project: str = "default", chat: str | None = None
) -> dict:
    if chat:
        existing = get_active_session(conn, chat=chat)
        if existing:
            raise ValueError(
                f"Chat already has an active session: #{existing['id']} '{existing['title']}' "
                f"(started {existing['started_at']}). Call end_session or resume_session first."
            )
    else:
        active = get_active_session(conn)
        if active:
            raise ValueError(
                f"A session is already active: #{active['id']} '{active['title']}' "
                f"(started {active['started_at']}). Call end_session first."
            )
    now = utcnow()
    session_id = conn.execute(
        "INSERT INTO sessions(title, project, started_at, chat_id, last_activity_at)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (title, project or "default", now, chat, now),
    ).fetchone()["id"]
    if chat:
        _bind(conn, chat, session_id)
    row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
    return {
        "session": _d(row),
        "prior_context": _prior_context(conn, project or "default", title, session_id),
    }


def end_session(
    conn: psycopg.Connection, summary: str | None = None, chat: str | None = None
) -> dict:
    active = get_active_session(conn, chat=chat)
    if not active:
        raise ValueError("No active session.")
    conn.execute(
        "UPDATE sessions SET ended_at = %s, summary = COALESCE(%s, summary), "
        "closed_reason = 'explicit' WHERE id = %s",
        (utcnow(), summary, active["id"]),
    )
    conn.execute("DELETE FROM chat_bindings WHERE session_id = %s", (active["id"],))
    row = conn.execute("SELECT * FROM sessions WHERE id = %s", (active["id"],)).fetchone()
    return {"session": _d(row)}


def sweep_idle_sessions(
    conn: psycopg.Connection,
    idle_minutes: float | None = None,
    now: datetime | None = None,
    include_chat: str | None = None,
) -> dict:
    """Auto-close sessions idle for longer than idle_minutes.

    A session is idle when its last_activity_at predates the cutoff. Idle-closed
    sessions keep ended_at set but closed_reason='idle', so resume_session can
    distinguish them from explicitly ended ones. Sessions with a NULL
    last_activity_at (legacy rows) are never swept.
    """
    if idle_minutes is None:
        idle_minutes = float(
            os.environ.get("ENGINEER_LOGBOOK_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)
        )
    cutoff = _c._idle_cutoff(now or _c._now(), idle_minutes)
    sql = (
        "UPDATE sessions SET ended_at = %s, closed_reason = 'idle'"
        " WHERE ended_at IS NULL AND last_activity_at IS NOT NULL AND last_activity_at <= %s"
    )
    params: list[Any] = [utcnow(), cutoff]
    if include_chat:
        sql += " AND chat_id = %s"
        params.append(include_chat)
    cur = conn.execute(sql, params)
    closed = cur.rowcount
    # drop bindings for swept sessions
    conn.execute(
        "DELETE FROM chat_bindings WHERE session_id NOT IN"
        " (SELECT id FROM sessions WHERE ended_at IS NULL)"
    )
    return {"closed": closed, "idle_minutes": idle_minutes, "cutoff": cutoff}


def resume_session(
    conn: psycopg.Connection, session_id: int | None = None, chat: str | None = None
) -> dict:
    """Rebind this chat to an existing (idle-closed) session, reopening it.

    - No session_id: resume the chat's own most recent idle-closed session.
    - Explicit closes are final; sessions live in another chat stay put.
    """
    if not chat:
        raise ValueError("resume_session requires a chat id.")
    if session_id is not None:
        row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        if not row:
            raise ValueError(f"No session with id {session_id}.")
        session = _d(row)
    else:
        session = _d(
            conn.execute(
                "SELECT * FROM sessions WHERE chat_id = %s AND closed_reason = 'idle'"
                " ORDER BY ended_at DESC, id DESC LIMIT 1",
                (chat,),
            ).fetchone()
        )
        if not session:
            raise ValueError(
                f"No reopenable (idle-closed) session found for this chat. "
                f"Use start_session to begin a new one."
            )

    if session["ended_at"] is None:
        # already open — check who owns it
        if session["chat_id"] == chat:
            _bind(conn, chat, session["id"])  # re-bind (idempotent)
            return {"session": session, "resumed": True, "reopened": False}
        holder = conn.execute(
            "SELECT chat_id FROM chat_bindings WHERE session_id = %s", (session["id"],)
        ).fetchone()
        if holder:
            raise ValueError(
                f"Session #{session['id']} is active in another chat; it cannot be resumed here."
            )
        # orphaned active session (e.g. its chat process died) — adopt it
        _bind(conn, chat, session["id"])
        return {"session": session, "resumed": True, "reopened": False, "adopted": True}

    if session["closed_reason"] != "idle":
        reason = session["closed_reason"] or "explicit"
        raise ValueError(
            f"Session #{session['id']} was closed ({reason}); it is final and cannot be resumed."
        )

    # reopen the idle-closed session for this chat
    conn.execute(
        "UPDATE sessions SET ended_at = NULL, closed_reason = NULL, last_activity_at = %s"
        " WHERE id = %s",
        (utcnow(), session["id"]),
    )
    _bind(conn, chat, session["id"])
    row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session["id"],)).fetchone()
    return {"session": _d(row), "resumed": True, "reopened": True}


def get_session(conn: psycopg.Connection, session_id: int) -> dict:
    row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
    if not row:
        raise ValueError(f"No session with id {session_id}.")
    return _d(row)


def save_session_context(
    conn: psycopg.Connection, context: dict[str, Any], session_id: int | None = None,
    chat: str | None = None,
) -> dict:
    if session_id is not None:
        session = get_session(conn, session_id)
    else:
        session = get_active_session(conn, chat=chat)
        if not session:
            raise ValueError(
                "No active session. Start one with start_session or pass session_id."
            )
    conn.execute(
        "UPDATE sessions SET context = %s WHERE id = %s",
        (_c._dump_context(context), session["id"]),
    )
    keys = sorted(context.keys()) if isinstance(context, dict) else None
    return {"saved": {"session_id": session["id"], "context_keys": keys}}


def restore_session_context(
    conn: psycopg.Connection, session_id: int | None = None, chat: str | None = None
) -> dict:
    if session_id is not None:
        session = get_session(conn, session_id)
    else:
        session = get_active_session(conn, chat=chat)
        if not session:
            row = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            session = _d(row)
    if not session:
        raise ValueError("No sessions recorded yet.")
    context = json.loads(session["context"]) if session["context"] else None
    return {"session": session, "context": context}


def list_sessions(
    conn: psycopg.Connection, limit: int = 20, project: str | None = None
) -> dict:
    sql = "SELECT id, title, project, started_at, ended_at, summary, chat_id, closed_reason FROM sessions"
    params: list[Any] = []
    if project:
        sql += " WHERE project = %s"
        params.append(project)
    sql += " ORDER BY started_at DESC LIMIT %s"
    params.append(max(1, limit))
    sessions = _ds(conn.execute(sql, params).fetchall())
    for s in sessions:
        s["active"] = s["ended_at"] is None
    return {"sessions": sessions, "count": len(sessions)}


def heartbeat(conn: psycopg.Connection, chat: str) -> None:
    """Keep this chat's session fresh (called by the server on every call)."""
    conn.execute(
        "UPDATE sessions SET last_activity_at = %s WHERE id ="
        " (SELECT session_id FROM chat_bindings WHERE chat_id = %s)",
        (utcnow(), chat),
    )


# ---------------------------------------------------------------- logs


def add_log(
    conn: psycopg.Connection,
    message: str,
    category: str = "info",
    tags: list[str] | None = None,
    project: str | None = None,
    session_id: int | None = None,
    chat: str | None = None,
) -> dict:
    if not message or not message.strip():
        raise ValueError("message must not be empty.")
    if session_id is None:
        active = get_active_session(conn, chat=chat)
        session_id = active["id"] if active else None
    if session_id is not None:
        get_session(conn, session_id)
        conn.execute(
            "UPDATE sessions SET last_activity_at = %s WHERE id = %s",
            (utcnow(), session_id),
        )
    if not project:
        project = get_session(conn, session_id)["project"] if session_id else "default"
    new_id = conn.execute(
        "INSERT INTO logs(session_id, timestamp, project, category, message, tags)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (session_id, utcnow(), project, category or "info", message, _c._tag_str(tags)),
    ).fetchone()["id"]
    row = conn.execute("SELECT * FROM logs WHERE id = %s", (new_id,)).fetchone()
    return {"log": _d(row)}


def get_logs(
    conn: psycopg.Connection,
    since: str | None = None,
    until: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
    session_id: int | None = None,
    limit: int = 100,
) -> dict:
    sql = "SELECT * FROM logs"
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append("timestamp >= %s")
        params.append(_c._norm_time(since, upper=False))
    if until:
        where.append("timestamp <= %s")
        params.append(_c._norm_time(until, upper=True))
    if category:
        where.append("category = %s")
        params.append(category)
    if project:
        where.append("project = %s")
        params.append(project)
    if session_id is not None:
        where.append("session_id = %s")
        params.append(session_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp DESC LIMIT 5000"
    logs = _ds(conn.execute(sql, params).fetchall())
    if tags:
        logs = [log for log in logs if _c._has_tags(log["tags"], tags)]
    logs.sort(key=lambda log: log["timestamp"], reverse=True)
    logs = logs[: max(1, limit)]
    return {"logs": logs, "count": len(logs)}


# ---------------------------------------------------------------- notes


def add_note(
    conn: psycopg.Connection,
    title: str,
    content: str,
    priority: str = "normal",
    tags: list[str] | None = None,
    project: str = "default",
    pinned: bool = False,
) -> dict:
    if not title or not title.strip():
        raise ValueError("title must not be empty.")
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {VALID_PRIORITIES}.")
    now = utcnow()
    new_id = conn.execute(
        "INSERT INTO notes(title, content, priority, pinned, project, tags, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (title, content, priority, int(bool(pinned)), project or "default", _c._tag_str(tags), now, now),
    ).fetchone()["id"]
    row = conn.execute("SELECT * FROM notes WHERE id = %s", (new_id,)).fetchone()
    return {"note": _d(row)}


def update_note(
    conn: psycopg.Connection,
    note_id: int,
    title: str | None = None,
    content: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    project: str | None = None,
) -> dict:
    existing = conn.execute("SELECT * FROM notes WHERE id = %s", (note_id,)).fetchone()
    if not existing:
        raise ValueError(f"No note with id {note_id}.")
    if priority is not None and priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {VALID_PRIORITIES}.")
    sets: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("title", title),
        ("content", content),
        ("priority", priority),
        ("pinned", None if pinned is None else int(bool(pinned))),
        ("project", project),
    ):
        if value is not None:
            sets.append(f"{column} = %s")
            params.append(value)
    if tags is not None:
        sets.append("tags = %s")
        params.append(_c._tag_str(tags))
    if not sets:
        raise ValueError("Nothing to update; pass at least one field.")
    sets.append("updated_at = %s")
    params.append(utcnow())
    params.append(note_id)
    conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = %s", params)
    row = conn.execute("SELECT * FROM notes WHERE id = %s", (note_id,)).fetchone()
    return {"note": _d(row)}


def list_notes(
    conn: psycopg.Connection,
    project: str | None = None,
    tag: str | None = None,
    pinned_only: bool = False,
    limit: int = 50,
) -> dict:
    sql = "SELECT * FROM notes"
    where: list[str] = []
    params: list[Any] = []
    if project:
        where.append("project = %s")
        params.append(project)
    if pinned_only:
        where.append("pinned = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pinned DESC, updated_at DESC LIMIT 2000"
    notes = _ds(conn.execute(sql, params).fetchall())
    if tag:
        notes = [n for n in notes if tag.strip().lower() in {t.lower() for t in n["tags"].split(",") if t.strip()}]
    notes = notes[: max(1, limit)]
    return {"notes": notes, "count": len(notes)}


def pin_note(conn: psycopg.Connection, note_id: int, pinned: bool = True) -> dict:
    return update_note(conn, note_id=note_id, pinned=pinned)


def delete_note(conn: psycopg.Connection, note_id: int) -> dict:
    cur = conn.execute("DELETE FROM notes WHERE id = %s", (note_id,))
    if cur.rowcount == 0:
        raise ValueError(f"No note with id {note_id}.")
    return {"deleted": note_id}


# ---------------------------------------------------------------- paths


def remember_path(
    conn: psycopg.Connection,
    path: str,
    kind: str | None = None,
    description: str | None = None,
    project: str | None = None,
) -> dict:
    p = path.strip()
    if not p:
        raise ValueError("path must not be empty.")
    now = utcnow()
    existing = conn.execute("SELECT * FROM paths WHERE path = %s", (p,)).fetchone()
    if existing:
        sets: list[str] = ["last_referenced = %s"]
        params: list[Any] = [now]
        if kind:
            sets.append("kind = %s")
            params.append(kind)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if project:
            sets.append("project = %s")
            params.append(project)
        params.append(existing["id"])
        conn.execute(f"UPDATE paths SET {', '.join(sets)} WHERE id = %s", params)
    else:
        conn.execute(
            "INSERT INTO paths(path, kind, description, project, last_referenced)"
            " VALUES (%s, %s, %s, %s, %s)",
            (p, kind or "dir", description, project or "default", now),
        )
    row = conn.execute("SELECT * FROM paths WHERE path = %s", (p,)).fetchone()
    return {"path_entry": _d(row)}


def list_paths(
    conn: psycopg.Connection, project: str | None = None, query: str | None = None, limit: int = 100
) -> dict:
    sql = "SELECT * FROM paths"
    where: list[str] = []
    params: list[Any] = []
    if project:
        where.append("project = %s")
        params.append(project)
    if query:
        like = f"%{query}%"
        where.append("(path ILIKE %s OR description ILIKE %s OR project ILIKE %s)")
        params.extend([like, like, like])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_referenced DESC LIMIT %s"
    params.append(max(1, limit))
    paths = _ds(conn.execute(sql, params).fetchall())
    return {"paths": paths, "count": len(paths)}


def forget_path(conn: psycopg.Connection, identifier: str) -> dict:
    ident = identifier.strip()
    if ident.isdigit():
        cur = conn.execute("DELETE FROM paths WHERE id = %s", (int(ident),))
    else:
        stripped = ident.rstrip("/")
        cur = conn.execute(
            "DELETE FROM paths WHERE path IN (%s, %s)", (stripped, stripped + "/")
        )
    if cur.rowcount == 0:
        raise ValueError(f"No remembered path matching {identifier!r}.")
    return {"deleted": ident}


# ---------------------------------------------------------------- tasks


def add_task(
    conn: psycopg.Connection,
    description: str,
    due_date: str | None = None,
    project: str = "default",
) -> dict:
    if not description or not description.strip():
        raise ValueError("description must not be empty.")
    if due_date:
        _c._validate_date(due_date, "due_date")
    new_id = conn.execute(
        "INSERT INTO tasks(description, due_date, project, created_at) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (description, due_date, project or "default", utcnow()),
    ).fetchone()["id"]
    row = conn.execute("SELECT * FROM tasks WHERE id = %s", (new_id,)).fetchone()
    return {"task": _d(row)}


def complete_task(conn: psycopg.Connection, task_id: int) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"No task with id {task_id}.")
    if row["status"] != "done":
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = %s WHERE id = %s",
            (utcnow(), task_id),
        )
    row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    return {"task": _d(row)}


def list_tasks(
    conn: psycopg.Connection, status: str | None = None, project: str | None = None, limit: int = 100
) -> dict:
    if status is not None and status not in VALID_TASK_STATUSES:
        raise ValueError(f"status must be one of {VALID_TASK_STATUSES}.")
    sql = "SELECT * FROM tasks"
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    if project:
        where.append("project = %s")
        params.append(project)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (
        " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC LIMIT %s"
    )
    params.append(max(1, limit))
    tasks = _ds(conn.execute(sql, params).fetchall())
    return {"tasks": tasks, "count": len(tasks)}


# ---------------------------------------------------------------- decisions


def add_decision(
    conn: psycopg.Connection,
    title: str,
    decision: str,
    rationale: str | None = None,
    alternatives: str | None = None,
    project: str = "default",
) -> dict:
    if not title or not title.strip():
        raise ValueError("title must not be empty.")
    if not decision or not decision.strip():
        raise ValueError("decision must not be empty.")
    new_id = conn.execute(
        "INSERT INTO decisions(title, decision, rationale, alternatives, project, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (title, decision, rationale, alternatives, project or "default", utcnow()),
    ).fetchone()["id"]
    row = conn.execute("SELECT * FROM decisions WHERE id = %s", (new_id,)).fetchone()
    return {"decision": _d(row)}


def list_decisions(
    conn: psycopg.Connection, project: str | None = None, limit: int = 50
) -> dict:
    sql = "SELECT * FROM decisions"
    params: list[Any] = []
    if project:
        sql += " WHERE project = %s"
        params.append(project)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(max(1, limit))
    decisions = _ds(conn.execute(sql, params).fetchall())
    return {"decisions": decisions, "count": len(decisions)}


# ---------------------------------------------------------------- snippets


def add_snippet(
    conn: psycopg.Connection,
    code: str,
    language: str = "text",
    description: str | None = None,
    tags: list[str] | None = None,
    project: str = "default",
) -> dict:
    if not code or not code.strip():
        raise ValueError("code must not be empty.")
    new_id = conn.execute(
        "INSERT INTO snippets(language, code, description, project, tags, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (language or "text", code, description, project or "default", _c._tag_str(tags), utcnow()),
    ).fetchone()["id"]
    row = conn.execute("SELECT * FROM snippets WHERE id = %s", (new_id,)).fetchone()
    return {"snippet": _d(row)}


def find_snippets(
    conn: psycopg.Connection, query: str | None = None, project: str | None = None, limit: int = 20
) -> dict:
    if not query:
        sql = "SELECT * FROM snippets"
        params: list[Any] = []
        if project:
            sql += " WHERE project = %s"
            params.append(project)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, limit))
        snippets = _ds(conn.execute(sql, params).fetchall())
        return {"snippets": snippets, "count": len(snippets)}
    result = search(conn, query, types=["snippet"], project=project, limit=limit)
    snippets = [r["entry"] for r in result["results"]]
    return {"snippets": snippets, "count": len(snippets)}


# ---------------------------------------------------------------- search


def _fts_query(query: str) -> str:
    return _c._fts_query(query)


def search(
    conn: psycopg.Connection,
    query: str,
    types: list[str] | None = None,
    project: str | None = None,
    limit: int = 20,
) -> dict:
    if types:
        bad = sorted(set(types) - set(SEARCHABLE_TYPES))
        if bad:
            raise ValueError(f"Invalid types {bad}. Allowed: {list(SEARCHABLE_TYPES)}.")
    # plainto_tsquery ANDs every term (like an FTS AND query)
    match = " ".join(_c._fts_query(query).replace('"', "").split())
    sql = (
        "SELECT entry_type, ref_id, title, content, tags, project"
        " FROM search_index WHERE tsv @@ plainto_tsquery('simple', %s)"
    )
    params: list[Any] = [match]
    if types:
        sql += f" AND entry_type IN ({', '.join(['%s'] * len(types))})"
        params.extend(types)
    if project:
        sql += " AND project = %s"
        params.append(project)
    sql += " ORDER BY ts_rank(tsv, plainto_tsquery('simple', %s)) DESC LIMIT %s"
    params.extend([match, max(1, limit)])
    hits = conn.execute(sql, params).fetchall()
    results = []
    for hit in hits:
        table = _HYDRATE_TABLE[hit["entry_type"]]
        row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (hit["ref_id"],)).fetchone()
        results.append(
            {
                "type": hit["entry_type"],
                "id": hit["ref_id"],
                "title": hit["title"],
                "content": hit["content"],
                "tags": hit["tags"],
                "project": hit["project"],
                "entry": _d(row),
            }
        )
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------- reporting


def _fmt_hhmm(ts: str | None) -> str:
    return ts[11:16] if ts and len(ts) >= 16 else (ts or "")


def daily_summary(conn: psycopg.Connection, date: str | None = None, project: str | None = None) -> dict:
    date = _c._validate_date(date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    proj_sql = " AND project = %s" if project else ""
    proj_params: list[Any] = [project] if project else []

    logs = _ds(
        conn.execute(
            f"SELECT * FROM logs WHERE substr(timestamp, 1, 10) = %s{proj_sql}"
            " ORDER BY timestamp",
            [date, *proj_params],
        ).fetchall()
    )
    sessions = _ds(
        conn.execute(
            f"SELECT * FROM sessions WHERE substr(started_at, 1, 10) = %s{proj_sql}"
            " ORDER BY started_at",
            [date, *proj_params],
        ).fetchall()
    )
    decisions = _ds(
        conn.execute(
            f"SELECT * FROM decisions WHERE substr(created_at, 1, 10) = %s{proj_sql}"
            " ORDER BY created_at",
            [date, *proj_params],
        ).fetchall()
    )
    notes = _ds(
        conn.execute(
            f"SELECT * FROM notes WHERE substr(created_at, 1, 10) = %s{proj_sql}"
            " ORDER BY created_at",
            [date, *proj_params],
        ).fetchall()
    )
    tasks_completed = _ds(
        conn.execute(
            f"SELECT * FROM tasks WHERE status = 'done' AND substr(completed_at, 1, 10) = %s{proj_sql}"
            " ORDER BY completed_at",
            [date, *proj_params],
        ).fetchall()
    )
    tasks_open = _ds(
        conn.execute(
            f"SELECT * FROM tasks WHERE status = 'open'{proj_sql} ORDER BY created_at DESC",
            [*proj_params],
        ).fetchall()
    )

    lines = [f"Logbook summary for {date} ({project or 'all projects'})"]
    if sessions:
        lines.append(
            "Sessions: " + ", ".join(f"#{s['id']} '{s['title']}'" for s in sessions)
        )
    else:
        lines.append("Sessions: none")
    lines.append(f"Logs ({len(logs)}):")
    lines.extend(f"  - {_fmt_hhmm(l['timestamp'])} [{l['category']}] {l['message']}" for l in logs)
    lines.append(f"Decisions ({len(decisions)}):")
    lines.extend(f"  - #{d['id']} {d['title']}" for d in decisions)
    lines.append(f"Notes created ({len(notes)}):")
    lines.extend(f"  - #{n['id']} {n['title']}" for n in notes)
    lines.append(f"Tasks completed ({len(tasks_completed)}):")
    lines.extend(f"  - {t['description']}" for t in tasks_completed)
    lines.append(f"Open tasks ({len(tasks_open)}):")
    lines.extend(f"  - {t['description']}" for t in tasks_open)

    workflow_nag = _workflow_nag(conn, project) if project else None
    if workflow_nag:
        lines.append(f"Spec workflow: {workflow_nag}")

    text = "\n".join(lines)

    return {
        "summary": {
            "date": date,
            "project": project or "all",
            "sessions": sessions,
            "logs": logs,
            "logs_count": len(logs),
            "decisions": decisions,
            "notes": notes,
            "tasks_completed": tasks_completed,
            "tasks_open": tasks_open,
            "text": text,
        }
    }


def stats(conn: psycopg.Connection) -> dict:
    tables = ("sessions", "logs", "notes", "paths", "tasks", "decisions", "snippets")
    counts = {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"] for t in tables
    }
    today = datetime.now(timezone.utc).date()
    activity = []
    for offset in range(6, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM logs WHERE substr(timestamp, 1, 10) = %s", (day,)
        ).fetchone()["c"]
        activity.append({"date": day, "logs": n})
    tag_counter: Counter[str] = Counter()
    for table in ("logs", "notes", "snippets"):
        for row in conn.execute(f"SELECT tags FROM {table} WHERE tags != ''").fetchall():
            for t in row["tags"].split(","):
                if t.strip():
                    tag_counter[t.strip().lower()] += 1
    active = get_active_session(conn)
    return {
        "stats": {
            "counts": counts,
            "activity_last_7_days": activity,
            "top_tags": dict(tag_counter.most_common(10)),
            "active_session": {"id": active["id"], "title": active["title"]} if active else None,
        }
    }


def export_markdown(
    conn: psycopg.Connection,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    lo = _c._norm_time(since, upper=False) if since else None
    hi = _c._norm_time(until, upper=True) if until else None
    proj_sql = " AND project = %s" if project else ""
    proj_params: list[Any] = [project] if project else []

    def ranged(column: str) -> tuple[str, list[Any]]:
        sql = ""
        params: list[Any] = []
        if lo:
            sql += f" AND {column} >= %s"
            params.append(lo)
        if hi:
            sql += f" AND {column} <= %s"
            params.append(hi)
        return sql, params

    scope = f"**{project or 'all projects'}**"
    range_txt = f" · {since or 'beginning'} → {until or 'now'}"
    parts = [f"# Engineer logbook — {scope}{range_txt}", ""]

    where_extra, extra_params = ranged("started_at")
    sessions = _ds(
        conn.execute(
            f"SELECT * FROM sessions WHERE 1=1{proj_sql}{where_extra} ORDER BY started_at",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Sessions")
    if sessions:
        for s in sessions:
            status = "active" if s["ended_at"] is None else f"ended {s['ended_at']}"
            parts.append(f"### #{s['id']} {s['title']}")
            parts.append(f"- project: {s['project']} · started: {s['started_at']} · {status}")
            if s["summary"]:
                parts.append(f"- summary: {s['summary']}")
            parts.append("")
    else:
        parts.extend(["_none_", ""])

    where_extra, extra_params = ranged("timestamp")
    logs = _ds(
        conn.execute(
            f"SELECT * FROM logs WHERE 1=1{proj_sql}{where_extra} ORDER BY timestamp",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Logs")
    if logs:
        for l in logs:
            tags = f" #{l['tags']}" if l["tags"] else ""
            parts.append(f"- `{l['timestamp']}` **[{l['category']}]** {l['message']}{tags}")
        parts.append("")
    else:
        parts.extend(["_none_", ""])

    where_extra, extra_params = ranged("created_at")
    notes = _ds(
        conn.execute(
            f"SELECT * FROM notes WHERE 1=1{proj_sql}{where_extra} ORDER BY created_at",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Notes")
    if notes:
        for n in notes:
            flags = []
            if n["pinned"]:
                flags.append("pinned")
            flags.append(n["priority"])
            tags = f" · tags: {n['tags']}" if n["tags"] else ""
            parts.append(f"### {n['title']} ({', '.join(flags)}){tags}")
            parts.append(n["content"])
            parts.append("")
    else:
        parts.extend(["_none_", ""])

    where_extra, extra_params = ranged("created_at")
    decisions = _ds(
        conn.execute(
            f"SELECT * FROM decisions WHERE 1=1{proj_sql}{where_extra} ORDER BY created_at",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Decisions")
    if decisions:
        for d in decisions:
            parts.append(f"### {d['title']}")
            parts.append(f"- decision: {d['decision']}")
            if d["rationale"]:
                parts.append(f"- rationale: {d['rationale']}")
            if d["alternatives"]:
                parts.append(f"- alternatives: {d['alternatives']}")
            parts.append("")
    else:
        parts.extend(["_none_", ""])

    where_extra, extra_params = ranged("created_at")
    snippets = _ds(
        conn.execute(
            f"SELECT * FROM snippets WHERE 1=1{proj_sql}{where_extra} ORDER BY created_at",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Snippets")
    if snippets:
        for s in snippets:
            parts.append(f"### {s['description'] or s['language']} ({s['language']})")
            parts.append(f"```{s['language']}")
            parts.append(s["code"])
            parts.append("```")
            parts.append("")
    else:
        parts.extend(["_none_", ""])

    where_extra, extra_params = ranged("created_at")
    tasks = _ds(
        conn.execute(
            f"SELECT * FROM tasks WHERE 1=1{proj_sql}{where_extra}"
            " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at",
            [*proj_params, *extra_params],
        ).fetchall()
    )
    parts.append("## Tasks")
    if tasks:
        for t in tasks:
            if t["status"] == "done":
                mark = "x"
                extra = f" (completed {t['completed_at']})"
            else:
                mark = " "
                extra = f" (due {t['due_date']})" if t["due_date"] else ""
            parts.append(f"- [{mark}] {t['description']}{extra}")
        parts.append("")
    else:
        parts.extend(["_none_", ""])

    counts = {
        "sessions": len(sessions),
        "logs": len(logs),
        "notes": len(notes),
        "decisions": len(decisions),
        "snippets": len(snippets),
        "tasks": len(tasks),
    }
    return {"markdown": "\n".join(parts).rstrip() + "\n", "counts": counts}