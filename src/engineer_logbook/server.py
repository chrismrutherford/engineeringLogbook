"""Engineer logbook MCP server.

A personal engineering logbook: work sessions with restorable context,
timestamped logs, durable notes, remembered paths, tasks, decisions (mini-ADRs),
reusable snippets, full-text search, daily summaries, markdown export, stats.
"""

from __future__ import annotations

import argparse
import contextvars
import os
import sys
from contextlib import contextmanager

import anyio
import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from engineer_logbook import db, schema_version

_IDLE_ENV = "ENGINEER_LOGBOOK_IDLE_MINUTES"
if _IDLE_ENV in os.environ:
    try:
        float(os.environ[_IDLE_ENV])
    except ValueError:
        print(
            f"engineer-logbook: ignoring invalid {_IDLE_ENV}="
            f"{os.environ[_IDLE_ENV]!r}; using default ({db.DEFAULT_IDLE_MINUTES})",
            file=sys.stderr,
            flush=True,
        )
        del os.environ[_IDLE_ENV]


# Chat identity of the tool call being served, pinned for the worker thread
# that runs its database work (see _run).
_CURRENT_CHAT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "engineer_logbook_chat", default=None
)


def _resolve_chat() -> str | None:
    """Identity of the calling chat for the current request.

    - streamable HTTP: one server serves many clients, so each MCP session
      (Mcp-Session-Id header) is its own chat.
    - stdio: one process per chat, so the process identity (db.chat_id()).
    - ENGINEER_LOGBOOK_LEGACY_GLOBAL=1: None (single shared session).
    """
    if os.environ.get("ENGINEER_LOGBOOK_LEGACY_GLOBAL") == "1":
        return None
    try:
        request = mcp.get_context().request_context.request
    except (LookupError, ValueError, AttributeError):
        request = None  # outside a request (or stdio): no HTTP request object
    session_id = request.headers.get("mcp-session-id") if request is not None else None
    if session_id:
        return f"http-{session_id}"
    return db.chat_id()


def _chat() -> str | None:
    """Chat identity for this tool call (pinned value inside worker threads)."""
    pinned = _CURRENT_CHAT.get()
    return pinned if pinned is not None else _resolve_chat()


mcp = FastMCP(
    "engineer-logbook",
    instructions=(
        "Personal engineering logbook backed by Postgres. ALWAYS use this "
        "logbook while doing engineering work — do not wait to be asked.\n"
        "How to use it:\n"
        "- At the start of any work, call get_current_session. If no session is "
        "active, start one with start_session for the project being worked on. "
        "When switching to a NEW or different project, end the current session "
        "and start a new one for that project — never mix projects in one session.\n"
        "- Multiple chats can run at once: sessions are scoped per chat. If your "
        "session was auto-closed after ~60 idle minutes, reopen it with "
        "resume_session (context is kept). Explicitly ended sessions are final.\n"
        "- While working, ALWAYS add_log for work activities as they happen: "
        "bugs found, fixes applied, commands run, tests written, meetings, "
        "blockers, ideas. Log in the moment, not retroactively in batches.\n"
        "- Record durable knowledge proactively: add_decision for any technical "
        "choice (what/why/alternatives), remember_path for important dirs/repos "
        "touched, add_snippet for reusable commands, add_task for follow-ups.\n"
        "- Before stopping or context-switching, save_session_context (branch, "
        "open files, next steps) and end_session with a wrap-up summary.\n"
        "- When resuming work, restore_session_context first; for standups or "
        "recaps use daily_summary, search, and list_sessions.\n"
        "- Spec workflows — MANDATORY for building or changing software: the "
        "logbook runs a V-model workflow (Spec-Driven Development). When the "
        "user asks for a feature or app, do NOT start coding: call spec_start"
        "(process='sdd', project=<repo>, feature=<short name>) and follow the "
        "phases via spec_step, which returns the prompt to execute and refuses "
        "when prerequisites are incomplete. Left leg (no code): constitution, "
        "specify, plan, tasks fetch templates with spec_template; report written "
        "artifacts with spec_report_artifact and mark steps done with "
        "spec_complete_step (evidence-based: it refuses while required files "
        "like tests or verification reports are unrecorded). Right side: "
        "mini-Vs — implement writes code WITH unit tests and runs them; converge "
        "verifies the increment against the spec (system-level); repeat implement/"
        "converge until every spec criterion passes, then ask the user to review "
        "and sign off, and only then spec_finish(outcome='sign-off: ...'). "
        "A start_session in a project with an active workflow will nudge you "
        "with the next step — follow it, do not jump ahead.\n"
        "Workflow tools: start_session when beginning work; add_log as you go "
        "(categories: info, bug, fix, idea, meeting, blocker); end_session with "
        "a summary.\n"
        "All tools accept an optional 'project' scoping field. search() spans "
        "logs, notes, decisions, snippets, and spec_artifacts. All responses "
        "are JSON dicts with an 'ok' boolean; check 'ok' before using other fields."
    ),
)


@contextmanager
def _cx():
    """Checkout from the module-level Postgres connection pool.

    Checked-out connections are validated by the pool (db_pg._conn_ok);
    if the connection breaks mid-operation the rollback is attempted and
    the connection is closed on return so the pool replaces it — a dead
    connection never comes back to block the next tool call.
    """
    conn = db.pool().getconn()
    broken = False
    try:
        yield conn
        conn.commit()
    except (db.psycopg.OperationalError, db.psycopg.InterfaceError):
        broken = True
        raise
    finally:
        if broken:
            # psycopg_pool >= 3.3 dropped putconn(close=...): closing the
            # connection ourselves makes the pool discard/replace it.
            try:
                conn.close()
            except Exception:
                pass
        db.pool().putconn(conn)


def _run_sync(op, chat: str | None) -> dict:
    try:
        with _cx() as conn:
            if chat:
                # heartbeat: keep this chat's session fresh, reap stale ones
                db.heartbeat(conn, chat)
                db.sweep_idle_sessions(conn)
            return {"ok": True, **op(conn)}
    except ValueError as e:  # includes SpecOpError
        return {"ok": False, "error": str(e)}


async def _run(op) -> dict:
    """Run a tool's database work in a worker thread.

    Tools are async so the event loop never blocks on Postgres: with one HTTP
    server serving many clients, a sync tool would stall every client for
    each remote round trip. The chat identity is resolved here (where the
    request context lives) and pinned into the thread's context so code
    calling _chat() inside `op` sees the same value.
    """
    chat = _resolve_chat()
    ctx = contextvars.copy_context()
    ctx.run(_CURRENT_CHAT.set, chat)
    return await anyio.to_thread.run_sync(ctx.run, _run_sync, op, chat)


# ---------------------------------------------------------------- sessions


@mcp.tool()
async def start_session(title: str, project: str = "default") -> dict:
    """Start a new work session for this chat (other chats are unaffected).

    Sessions group log entries and hold restorable working context. The
    session auto-closes after ~60 idle minutes and can be reopened with
    resume_session.

    The response includes 'prior_context': recent sessions for the same
    project, similar sessions found by title keywords, project notes (pinned
    and high-priority first), and open tasks. READ IT FIRST — if a related
    session already covers this work, consider resume_session(id) with
    restore_session_context instead of redoing it.
    """
    return await _run(lambda c: db.start_session(c, title=title, project=project, chat=_chat()))


@mcp.tool()
async def end_session(summary: str | None = None) -> dict:
    """End this chat's active session (final; not reopenable), with summary."""
    return await _run(lambda c: db.end_session(c, summary=summary, chat=_chat()))


@mcp.tool()
async def get_current_session() -> dict:
    """Get this chat's active session, or session=null when idle."""
    return await _run(lambda c: {"session": db.get_active_session(c, chat=_chat())})


@mcp.tool()
async def save_session_context(context: dict, session_id: int | None = None) -> dict:
    """Persist working context (branch, open files, plans, TODOs) as JSON.

    Targets the active session, or the given session_id. Restore later with
    restore_session_context.
    """
    return await _run(
        lambda c: db.save_session_context(c, context=context, session_id=session_id, chat=_chat())
    )


@mcp.tool()
async def restore_session_context(session_id: int | None = None) -> dict:
    """Restore saved context from the active, given, or most recent session.

    Returns {session, context}. Falls back to the latest session when none is
    active and no session_id is given.
    """
    return await _run(
        lambda c: db.restore_session_context(c, session_id=session_id, chat=_chat())
    )


@mcp.tool()
async def list_sessions(limit: int = 20, project: str | None = None) -> dict:
    """List recent sessions (newest first) with an 'active' flag and summaries."""
    return await _run(lambda c: db.list_sessions(c, limit=limit, project=project))


@mcp.tool()
async def resume_session(session_id: int | None = None) -> dict:
    """Reopen this chat's most recent idle-closed session (or a specific one).

    The active session is auto-closed after ~60 idle minutes, but not lost:
    resume_session reopens it (cleared ended_at, saved context intact).
    Explicit end_session closes are final. Pass session_id to resume a
    specific session; omit it to resume the latest idle-closed one.
    """
    chat = _chat()
    if chat is None:
        return {"ok": False, "error": "resume_session requires chat mode."}
    return await _run(lambda c: db.resume_session(c, session_id=session_id, chat=chat))


@mcp.tool()
async def close_idle_sessions(minutes: float | None = None) -> dict:
    """Manually sweep and idle-close sessions idle for >minutes (default 60).

    Normally automatic: every tool call heartbeats the chat's session and
    sweeps stale ones. Sessions closed this way are reopenable via
    resume_session; explicitly ended sessions are not.
    """
    return await _run(lambda c: db.sweep_idle_sessions(c, idle_minutes=minutes))


# ---------------------------------------------------------------- logs


@mcp.tool()
async def add_log(
    message: str,
    category: str = "info",
    tags: list[str] | None = None,
    project: str | None = None,
    session_id: int | None = None,
) -> dict:
    """Append a timestamped log entry.

    Suggested categories: info, bug, fix, idea, meeting, blocker.
    Auto-attaches to this chat's active session and inherits its project; pass
    project/session_id to override.
    """
    return await _run(
        lambda c: db.add_log(
            c, message=message, category=category, tags=tags, project=project,
            session_id=session_id, chat=_chat(),
        )
    )


@mcp.tool()
async def get_logs(
    since: str | None = None,
    until: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
    session_id: int | None = None,
    limit: int = 100,
) -> dict:
    """Query log entries, newest first.

    since/until take YYYY-MM-DD or full ISO timestamps; tags filter is an
    exact, case-insensitive subset match.
    """
    return await _run(
        lambda c: db.get_logs(
            c,
            since=since,
            until=until,
            category=category,
            tags=tags,
            project=project,
            session_id=session_id,
            limit=limit,
        )
    )


# ---------------------------------------------------------------- notes


@mcp.tool()
async def add_note(
    title: str,
    content: str,
    priority: str = "normal",
    tags: list[str] | None = None,
    project: str = "default",
    pinned: bool = False,
) -> dict:
    """Create a durable note (priority: low/normal/high; optionally pinned)."""
    return await _run(
        lambda c: db.add_note(
            c, title=title, content=content, priority=priority, tags=tags, project=project, pinned=pinned
        )
    )


@mcp.tool()
async def update_note(
    note_id: int,
    title: str | None = None,
    content: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    project: str | None = None,
) -> dict:
    """Update fields of an existing note. Only provided fields change.

    Pass tags=[] to clear tags; tags=None leaves them untouched.
    """
    return await _run(
        lambda c: db.update_note(
            c, note_id=note_id, title=title, content=content, priority=priority,
            tags=tags, pinned=pinned, project=project,
        )
    )


@mcp.tool()
async def list_notes(
    project: str | None = None,
    tag: str | None = None,
    pinned_only: bool = False,
    limit: int = 50,
) -> dict:
    """List notes (pinned first, newest updates first) with filters."""
    return await _run(lambda c: db.list_notes(c, project=project, tag=tag, pinned_only=pinned_only, limit=limit))


@mcp.tool()
async def pin_note(note_id: int, pinned: bool = True) -> dict:
    """Pin or unpin a note. Pinned notes surface first in list_notes."""
    return await _run(lambda c: db.pin_note(c, note_id=note_id, pinned=pinned))


@mcp.tool()
async def delete_note(note_id: int) -> dict:
    """Permanently delete a note by id."""
    return await _run(lambda c: db.delete_note(c, note_id=note_id))


# ---------------------------------------------------------------- paths


@mcp.tool()
async def remember_path(
    path: str,
    kind: str | None = None,
    description: str | None = None,
    project: str | None = None,
) -> dict:
    """Remember an important file/dir/repo/URL with an optional description.

    Re-remembering the same path refreshes last_referenced and updates any
    provided fields. kind: file, dir, repo, url (default dir).
    """
    return await _run(lambda c: db.remember_path(c, path=path, kind=kind, description=description, project=project))


@mcp.tool()
async def list_paths(project: str | None = None, query: str | None = None, limit: int = 100) -> dict:
    """List remembered paths (most recently referenced first).

    query does a substring match on path/description/project.
    """
    return await _run(lambda c: db.list_paths(c, project=project, query=query, limit=limit))


@mcp.tool()
async def forget_path(identifier: str) -> dict:
    """Forget a remembered path by its path string (or numeric id)."""
    return await _run(lambda c: db.forget_path(c, identifier=identifier))


# ---------------------------------------------------------------- tasks


@mcp.tool()
async def add_task(description: str, due_date: str | None = None, project: str = "default") -> dict:
    """Add a TODO task, optionally with a due date (YYYY-MM-DD)."""
    return await _run(lambda c: db.add_task(c, description=description, due_date=due_date, project=project))


@mcp.tool()
async def complete_task(task_id: int) -> dict:
    """Mark a task done (records completed_at). Idempotent."""
    return await _run(lambda c: db.complete_task(c, task_id=task_id))


@mcp.tool()
async def list_tasks(status: str | None = None, project: str | None = None, limit: int = 100) -> dict:
    """List tasks; open tasks first. status: open | done."""
    return await _run(lambda c: db.list_tasks(c, status=status, project=project, limit=limit))


# ---------------------------------------------------------------- decisions


@mcp.tool()
async def add_decision(
    title: str,
    decision: str,
    rationale: str | None = None,
    alternatives: str | None = None,
    project: str = "default",
) -> dict:
    """Record a technical decision (mini-ADR): what, why, and alternatives."""
    return await _run(
        lambda c: db.add_decision(
            c, title=title, decision=decision, rationale=rationale,
            alternatives=alternatives, project=project,
        )
    )


@mcp.tool()
async def list_decisions(project: str | None = None, limit: int = 50) -> dict:
    """List recorded decisions, newest first."""
    return await _run(lambda c: db.list_decisions(c, project=project, limit=limit))


# ---------------------------------------------------------------- snippets


@mcp.tool()
async def add_snippet(
    code: str,
    language: str = "text",
    description: str | None = None,
    tags: list[str] | None = None,
    project: str = "default",
) -> dict:
    """Store a reusable code snippet, command, or config with language/description."""
    return await _run(
        lambda c: db.add_snippet(
            c, code=code, language=language, description=description, tags=tags, project=project
        )
    )


@mcp.tool()
async def find_snippets(query: str | None = None, project: str | None = None, limit: int = 20) -> dict:
    """Find stored snippets by keyword (code, description, tags); lists all without query."""
    return await _run(lambda c: db.find_snippets(c, query=query, project=project, limit=limit))


# ---------------------------------------------------------------- spec workflows


async def _spec_run(op) -> dict:
    """Spec operations dispatch like every tool (SpecOpError is a ValueError,
    so it maps to {'ok': False, 'error': ...}); they also heartbeat the chat."""
    return await _run(op)


@mcp.tool()
async def spec_start(
    process: str,
    project: str = "default",
    feature: str | None = None,
    slug: str | None = None,
    folder: str | None = None,
) -> dict:
    """Start a spec workflow — REQUIRED before building or changing software
    (sdd for features/apps, bugfix for bugs, assess for ideas).

    Call this FIRST when the user asks to build something (e.g. 'make a
    tetris game'): process='sdd', project=<repo name>, feature=<short name>.
    Never write code before the workflow reaches the implement step. One
    active workflow per project; starting a new one replaces the old.
    Artifacts live under the project folder ('specs' by default, e.g.
    'docs/specs'); the logbook tracks progress and snapshots — no other
    local state. Returns the workflow with its step plan and next-step nag.
    """
    return await _spec_run(
        lambda c: db.spec_start(
            c, process=process, project=project, feature=feature, slug=slug, folder=folder
        )
    )


@mcp.tool()
async def spec_step(
    process: str,
    step: str,
    args: str = "",
    project: str = "default",
    folder: str | None = None,
) -> dict:
    """Get the executable prompt for one workflow step, prerequisites enforced.

    Delivers the rendered prompt (args substituted into its User Input
    block); refuses until prerequisite steps are complete. Execute the
    prompt, record artifacts, complete the step — the response's workflow.nag
    names the next step; keep following it until spec_finish. sdd is a
    V-model: constitution, specify, plan, tasks (left leg, no code); then
    mini-Vs of implement (code WITH unit tests) + converge (verify against
    the spec) until converged. bugfix: assess, fix, test. assess: intake,
    research, define, shape, decide.
    """
    return await _spec_run(
        lambda c: db.spec_step(
            c, process=process, step=step, args=args, project=project, folder=folder
        )
    )


@mcp.tool()
async def spec_template(name: str) -> dict:
    """Get a bundled artifact template: spec, plan, tasks, constitution, or checklist.

    Spec workflow step prompts say when to use one (e.g. the specify step
    starts spec.md from the 'spec' template).
    """
    try:
        return {"ok": True, **db.spec_template(name)}
    except db.SpecOpError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def spec_report_artifact(path: str, content: str, step: str, project: str = "default") -> dict:
    """Record an artifact (path + full content) for the active spec workflow.

    REQUIRED for every file a step produces — code, tests, docs alike
    (implementation files included; snapshots of code count toward the
    gates). Path must stay inside the workflow folder ('specs/' by default);
    snapshots are full-text searchable. Later calls with the same path
    refresh the snapshot.
    """
    return await _spec_run(
        lambda c: db.spec_report_artifact(c, path=path, content=content, step=step, project=project)
    )


@mcp.tool()
async def spec_complete_step(step: str, project: str = "default") -> dict:
    """Mark a spec workflow step complete — EVIDENCE-BASED, not self-report.

    Refuses until the step's required artifacts are recorded: constitution,
    specify, plan, tasks each need their .md; implement needs recorded test
    files (unit tests run green); converge needs a verification report of
    the increment against the spec. If the gate rejects you, the step is
    not done: finish the work, don't argue the gate.
    """
    return await _spec_run(lambda c: db.spec_complete_step(c, step=step, project=project))


@mcp.tool()
async def spec_progress(project: str = "default") -> dict:
    """Spec workflow state: completed steps, artifacts, recent events, next step."""
    return await _spec_run(lambda c: db.spec_progress(c, project=project))


@mcp.tool()
async def spec_finish(outcome: str, project: str = "default") -> dict:
    """Close the spec workflow — requires the USER'S SIGN-OFF in the outcome.

    Only after implement/converge report the spec passes and the user has
    reviewed and approved: outcome='sign-off: <what the user approved>'.
    Refuses an outcome without 'sign-off', and refuses while sdd steps are
    incomplete. Integration tests are out of scope for now.
    """
    return await _spec_run(lambda c: db.spec_finish(c, outcome=outcome, project=project))


# ---------------------------------------------------------------- search


@mcp.tool()
async def search(
    query: str,
    types: list[str] | None = None,
    project: str | None = None,
    limit: int = 20,
) -> dict:
    """Full-text search across the logbook (FTS5, ranked).

    types: subset of [log, note, decision, snippet]; each result includes the
    hydrated source entry. All query terms are ANDed.
    """
    return await _run(lambda c: db.search(c, query=query, types=types, project=project, limit=limit))


# ---------------------------------------------------------------- reporting


@mcp.tool()
async def daily_summary(date: str | None = None, project: str | None = None) -> dict:
    """Summarize a day's activity: sessions, logs, decisions, notes, tasks.

    date defaults to today (UTC); format YYYY-MM-DD. Includes a ready-to-paste
    text digest and the currently open tasks.
    """
    return await _run(lambda c: db.daily_summary(c, date=date, project=project))


@mcp.tool()
async def export_markdown(
    project: str | None = None, since: str | None = None, until: str | None = None
) -> dict:
    """Export a slice of the logbook as a markdown document.

    Filters everything by project and date range (YYYY-MM-DD or ISO timestamps).
    """
    return await _run(lambda c: db.export_markdown(c, project=project, since=since, until=until))


@mcp.tool()
async def stats() -> dict:
    """Logbook statistics: table counts, last-7-days activity, top tags."""
    return await _run(lambda c: db.stats(c))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    ap = argparse.ArgumentParser(
        prog="engineer-logbook",
        description="Engineer logbook MCP server (stdio for local clients, "
        "streamable HTTP for remote ones).",
    )
    ap.add_argument(
        "--transport", choices=("stdio", "streamable-http"),
        default=env("ENGINEER_LOGBOOK_TRANSPORT", "stdio"),
        help="default: stdio (env ENGINEER_LOGBOOK_TRANSPORT)",
    )
    ap.add_argument("--host", default=env("ENGINEER_LOGBOOK_HOST", "127.0.0.1"),
                    help="HTTP bind address (default 127.0.0.1; env ENGINEER_LOGBOOK_HOST)")
    ap.add_argument("--port", type=int, default=int(env("ENGINEER_LOGBOOK_PORT", "8000")),
                    help="HTTP port (default 8000; env ENGINEER_LOGBOOK_PORT)")
    ap.add_argument(
        "--allowed-host", action="append", dest="allowed_hosts",
        default=[h.strip() for h in env("ENGINEER_LOGBOOK_ALLOWED_HOSTS", "").split(",") if h.strip()],
        help="extra Host header accepted over HTTP (repeatable; e.g. logbook.example.com). "
        "DNS-rebinding protection otherwise only accepts localhost. "
        "Env ENGINEER_LOGBOOK_ALLOWED_HOSTS (comma-separated).",
    )
    return ap.parse_args(argv)


def _configure_http(args: argparse.Namespace) -> None:
    s = mcp.settings
    s.host, s.port = args.host, args.port
    # Stateful sessions: each client's Mcp-Session-Id is its chat identity
    # (_resolve_chat). Stateless mode would merge all clients into one chat.
    s.stateless_http = False
    for host in args.allowed_hosts:
        for h in (host, f"{host}:*"):
            if h not in s.transport_security.allowed_hosts:
                s.transport_security.allowed_hosts.append(h)
        for scheme in ("https", "http"):
            origin = f"{scheme}://{host}"
            if origin not in s.transport_security.allowed_origins:
                s.transport_security.allowed_origins.append(origin)


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server (stdio by default, or streamable HTTP).

    The server never changes the schema: it only checks that the database
    has every bundled migration applied (engineer-logbook-migrate up) and
    refuses to start otherwise, rather than failing later mid-tool-call.
    Diagnostics go to stderr; over stdio, stdout is the protocol channel.

    HTTP has NO authentication yet: keep it on 127.0.0.1 or a private
    network/tunnel until OAuth is added.
    """
    args = _parse_args(argv)
    try:
        with _cx() as conn:
            schema_version.require_current(conn)
    except schema_version.SchemaOutOfDate as e:
        print(f"engineer-logbook: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    if args.transport == "streamable-http":
        _configure_http(args)
        print(
            f"engineer-logbook: streamable HTTP on http://{args.host}:{args.port}"
            f"{mcp.settings.streamable_http_path} (no auth)",
            file=sys.stderr, flush=True,
        )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
