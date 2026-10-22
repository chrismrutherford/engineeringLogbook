# engineer-logbook

A personal **engineering logbook MCP server** — Python + PostgreSQL. Track
work sessions (with restorable working context), timestamped logs, durable
notes, important paths, tasks, technical decisions (mini-ADRs), reusable
snippets, and **spec-kit-style spec workflows** — all full-text searchable,
with daily summaries, next-step nags, and markdown export.

## Features

- **Spec workflows** — spec-driven development (specify → plan → tasks →
  implement → converge), bug fixing (assess → fix → test), and idea
  assessment (intake → ... → decide) with DB-tracked state, prerequisite
  gates, next-step nags at session start, and searchable artifact snapshots
  (artifacts themselves live in your repo under `specs/`).

- **Sessions** — start/stop work sessions; save and restore working context
  (branch, open files, plans) across restarts; wrap-up summaries.
- **Multi-chat aware** — many chats/clients can use the logbook at once: each
  connection owns its own active session. Sessions idle for ~60 minutes are
  auto-closed (heartbeat-based) and can be reopened later with `resume_session`,
  context intact. Explicitly ended sessions are final.
- **Logs** — timestamped entries with categories (`info`, `bug`, `fix`, `idea`,
  `meeting`, `blocker`) and tags; auto-attached to the active session.
- **Notes** — durable knowledge with priority + pinning.
- **Paths** — remember important dirs/repos/URLs with descriptions.
- **Tasks** — TODOs with due dates and completion tracking.
- **Decisions** — mini-ADRs: what was decided, why, and what else was considered.
- **Snippets** — reusable commands/config/code, searchable.
- **Search** — Postgres full-text search (tsvector + GIN, ranked) across
  logs, notes, decisions, snippets.
- **Reporting** — daily standup-style summaries, markdown export, usage stats.

Everything is optionally scoped by a `project` field. Each chat has at most one
active session; multiple chats may run concurrently. All tools return
`{"ok": bool, ...}` JSON.

## Install & run

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
cd /home/chris/repos/engineerLogBook
uv sync          # create venv, install deps, write uv.lock
uv run engineer-logbook   # starts the MCP server on stdio
uv run engineer-logbook-ui  # read-only terminal viewer
```

## Client configuration

### Claude Desktop / any MCP client (`mcpServers` style)

```json
{
  "mcpServers": {
    "engineer-logbook": {
      "command": "uv",
      "args": ["run", "--directory", "/home/chris/repos/engineerLogBook", "engineer-logbook"]
    }
  }
}
```

### opencode (`~/.config/opencode/opencode.json`)

```json
{
  "mcp": {
    "engineer-logbook": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/home/chris/repos/engineerLogBook", "engineer-logbook"]
    }
  }
}
```

## Remote server (streamable HTTP)

The same server runs over HTTP for remote clients:

```bash
uv run engineer-logbook --transport streamable-http --host 127.0.0.1 --port 8000
# endpoint: http://127.0.0.1:8000/mcp
```

| Option | Env | Default |
|---|---|---|
| `--transport stdio\|streamable-http` | `ENGINEER_LOGBOOK_TRANSPORT` | `stdio` |
| `--host` | `ENGINEER_LOGBOOK_HOST` | `127.0.0.1` |
| `--port` | `ENGINEER_LOGBOOK_PORT` | `8000` |
| `--allowed-host NAME` (repeatable) | `ENGINEER_LOGBOOK_ALLOWED_HOSTS` (comma-separated) | localhost only |

- **No authentication yet.** Anyone who can reach the port can read and write
  the whole logbook. Keep it on `127.0.0.1`, a private network, or behind a
  tunnel until OAuth is added.
- DNS-rebinding protection only accepts `localhost` Host headers; behind a
  domain/reverse proxy add it with `--allowed-host logbook.example.com`.
- Each client's MCP session (`Mcp-Session-Id`) is its own chat, so clients
  have independent sessions. Sessions are stateful; don't run it stateless.
- Tool calls run their database work in worker threads, so one slow call
  doesn't stall other clients. Run the server close to the database (same
  region): each call makes several round trips.
- Order of deployment: `engineer-logbook-migrate up`, then start the server.

## Concurrent chats & idle sessions

Each chat is identified separately — over stdio by its server process (one
process per chat), over HTTP by its MCP session — so several chats can be open
simultaneously, each with its own active session, without seeing or blocking
each other.

**Closing sessions that are out of use:** every tool call is a heartbeat that
refreshes the chat's session and sweeps sessions whose last activity is older
than the idle threshold (default 60 minutes). Swept sessions are marked
`closed_reason='idle'` and remain in the logbook.

**Reopening them:** the idle chat calls `resume_session` — the most recent
idle-closed session for that chat is reopened (its `ended_at` cleared, saved
context untouched). Rules:

- `resume_session` with no argument → reopen the chat's latest idle-closed session.
- `resume_session(session_id=N)` → reopen a specific session.
- Explicitly ended sessions (`end_session`) are final and cannot be resumed.
- A session still active in another (live) chat cannot be stolen; an orphaned
  active session (its chat process died) is adopted on resume.

Environment variables: `ENGINEER_LOGBOOK_IDLE_MINUTES` (default `60`) to change
the idle threshold, `ENGINEER_LOGBOOK_CHAT_ID` to pin a chat identity, and
`ENGINEER_LOGBOOK_LEGACY_GLOBAL=1` to restore the old single-shared-session
behaviour.

## Terminal UI (read-only viewer)

Browse the whole logbook in the terminal:

```bash
uv run engineer-logbook-ui
```

Tabs: **Logs, Notes, Sessions, Tasks, Decisions, Snippets, Paths, Summary, Search**.
Highlight rows (↑/↓ or mouse) for a full preview in the detail pane.

| Key | Action |
|---|---|
| `q` | quit |
| `r` | refresh from the database |
| `t` | jump to today's daily summary |
| `s` | jump to the search box (FTS across all content types) |
| `Enter` | pin the highlighted row's details |

The UI is read-only — safe to run alongside MCP sessions, which may be writing
concurrently (Postgres MVCC allows multi-reader).

## Tools (36)

| Group | Tools |
|---|---|
| Sessions | `start_session`, `end_session`, `get_current_session`, `save_session_context`, `restore_session_context`, `list_sessions`, `resume_session`, `close_idle_sessions` |
| Logs | `add_log`, `get_logs` |
| Notes | `add_note`, `update_note`, `list_notes`, `pin_note`, `delete_note` |
| Paths | `remember_path`, `list_paths`, `forget_path` |
| Tasks | `add_task`, `complete_task`, `list_tasks` |
| Decisions | `add_decision`, `list_decisions` |
| Snippets | `add_snippet`, `find_snippets` |
| Spec workflows | `spec_start`, `spec_step`, `spec_template`, `spec_report_artifact`, `spec_complete_step`, `spec_progress`, `spec_finish` |
| Search | `search` |
| Reporting | `daily_summary`, `export_markdown`, `stats` |

## Spec workflows (MCP tools only, DB-tracked)

Run spec-driven development, bug fixing, or idea assessment as a managed
workflow driven entirely through MCP tools: no slash commands, setup scripts,
hooks, or state files. The logbook tracks which step you're on, delivers each
step's executable prompt (prerequisites enforced), hands out templates, and
full-text-searches artifact snapshots. One **active workflow per project**;
`start_session` for that project nudges you with the next step.

```text
spec_start(process="sdd", feature="User Auth")      # or bugfix / assess
spec_step("sdd", "constitution")                    # returns the prompt to execute
spec_template("spec")                               # templates on request
  ... agent writes specs/constitution.md ...
spec_report_artifact("specs/constitution.md", "<full text>", step="constitution")
spec_complete_step("constitution")                  # -> next step
  ... constitution -> specify -> plan -> tasks ...
  ... then mini-Vs: implement (code + unit tests) -> converge (verify vs spec) ...
spec_finish(outcome="sign-off: user approved <...>")
```

- **V-model with mini-Vs.** Left leg (no code): constitution, specify, plan,
  tasks. Right side: repeat **implement** (write the next tasks WITH unit
  tests, run them) and **converge** (verify the increment against the spec at
  system level, append convergence tasks for failures) until every spec
  criterion passes. Integration/e2e tests are out of scope for now.
- **Evidence gates, not self-reporting.** `spec_complete_step` refuses while
  required artifacts are unrecorded: constitution/specify/plan/tasks need
  their `.md`; implement needs recorded test files; converge needs a
  verification report.
- **Sign-off required to finish.** `spec_finish` refuses any outcome without
  the user's sign-off (`outcome="sign-off: ..."`), and refuses while sdd
  mandatory steps are incomplete. The agent must show you the verification
  results and wait for approval.
- Processes and prerequisite chains: **sdd** (above; optional clarify and
  checklist after specify, analyze after tasks); **bugfix** assess -> fix ->
  test; **assess** intake -> research -> define -> shape -> decide. Steps
  after the first in bugfix/assess need `slug=` (remembered once given).
- Every prompt opens with a **Workflow context** block built from the DB
  (folder, feature directory, constitution path, completed steps, recorded
  artifacts) and ends with the recording protocol.
- Everything lives **in your repo under the workflow folder** (`specs/` by
  default, `folder="docs/specs"` to relocate): `specs/constitution.md`,
  `specs/NNN-feature/` (numbered per project by the logbook),
  `specs/bugs/<slug>/`, `specs/assessments/<slug>/`. The DB stores progress
  and a searchable snapshot per artifact; reported paths must stay inside
  the folder.
- Prompts and templates are adapted from
  [GitHub Spec Kit](https://github.com/github/spec-kit) (MIT) with its
  agent-integration plumbing removed; a test scans every rendered step for
  leftovers.

## Storage

PostgreSQL, via pooled psycopg3 connections. Point the server at a database
with either a DSN or the `POSTGRES_*` family:

```bash
export POSTGRES_HOST=db.example.com
export POSTGRES_PORT=25060
export POSTGRES_USER=chris
export POSTGRES_PASSWORD=...
export POSTGRES_DB=logbookdb        # default: logbookdb
export PGSSLMODE=require            # default: require
uv run engineer-logbook
```

or a single DSN: `export ENGINEER_LOGBOOK_PG_DSN="host=... dbname=logbookdb user=chris password=... sslmode=require"`.

Connections are pooled (psycopg_pool); each tool call checks a connection out,
commits, and returns it.

### Schema migrations

The schema lives in numbered SQL files in `src/engineer_logbook/migrations/`
and is changed **only** by the migration runner. The server never executes
DDL: at startup it checks that every bundled migration is applied and exits
(message on stderr) if the database is behind.

```bash
uv run engineer-logbook-migrate status          # applied / pending / drifted
uv run engineer-logbook-migrate up --dry-run    # what would run
uv run engineer-logbook-migrate up              # apply pending migrations
```

Each migration runs in its own transaction and is recorded in
`schema_migrations` with a checksum; an advisory lock serialises concurrent
runners. Never edit an applied migration (a checksum mismatch stops the run);
add a new numbered file instead. For a remote deployment, run `up` as a deploy
step before starting the server. Order matters: migrate, then restart servers.

Maintenance lives in the admin CLI (`init` is now an alias for `migrate up`):

```bash
uv run engineer-logbook-pg-admin reindex   # rebuild the search index from table rows
uv run engineer-logbook-pg-admin verify    # print table counts
```

### Users

Every table has a `user_id` (existing data belongs to `chris`, set by
migration 0003). New rows take their owner from the Postgres session setting
`logbook.user_id`, which the server sets on every connection from
`ENGINEER_LOGBOOK_USER_ID` (default `chris`); a connection without it cannot
insert. Uniqueness is per user (`paths`, active spec workflow per project).
Queries are **not yet isolated by user**: that's the row-level-security step
for when a second user (OAuth) exists.

Timestamps are ISO 8601 strings; tags are comma-separated strings.

## Tests

```bash
ENGINEER_LOGBOOK_PG_TEST_DSN="host=... dbname=logbookdb_test user=chris password=... sslmode=require" \
  uv run pytest
```

Tests run against a scratch database (`logbookdb_test` is meant for this —
each run drops every logbook object and rebuilds it through the migrations,
so never point it at real data); without `ENGINEER_LOGBOOK_PG_TEST_DSN` they skip. The
end-to-end tests spawn the real server over stdio and exercise the full
workflow (sessions → logs → notes → search → reports → context restore)
using the official MCP client.
