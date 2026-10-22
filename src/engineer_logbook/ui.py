"""Read-only Textual TUI for browsing the engineer logbook.

Run with:  engineer-logbook-ui   (or: uv run engineer-logbook-ui)

Keys:
    q        quit
    r        refresh all tabs from the database
    t        jump to today's daily summary
    s        jump to the search box
    ↑/↓      highlight rows (live preview in the detail pane)
    Enter    pin a row's details in the detail pane
"""

from __future__ import annotations

import json

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from engineer_logbook import db

_TABLES = ("logs", "notes", "sessions", "tasks", "decisions", "snippets", "paths")


def _short(text: str | None, width: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _dt(ts: str | None) -> str:
    return (ts or "").replace("T", " ")[:19]


def _build_logs(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("time", 20), ("project", 12), ("category", 9), ("message", 58), ("tags", 16)]
    entries = {}
    rows = []
    for log in db.get_logs(conn, limit=500)["logs"]:
        entries[str(log["id"])] = log
        rows.append(
            (str(log["id"]), _dt(log["timestamp"]), log["project"], log["category"],
             _short(log["message"], 56), log["tags"])
        )
    return cols, rows, entries


def _build_notes(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("priority", 8), ("pin", 4), ("title", 40), ("updated", 20), ("tags", 14)]
    entries = {}
    rows = []
    for n in db.list_notes(conn, limit=500)["notes"]:
        entries[str(n["id"])] = n
        rows.append(
            (str(n["id"]), n["project"], n["priority"], "pin" if n["pinned"] else "",
             _short(n["title"], 38), _dt(n["updated_at"]), n["tags"])
        )
    return cols, rows, entries


def _build_sessions(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("title", 30), ("started", 20), ("ended", 20),
            ("state", 8), ("chat", 14)]
    entries = {}
    rows = []
    for s in db.list_sessions(conn, limit=200)["sessions"]:
        entries[str(s["id"])] = s
        state = "ACTIVE" if not s["ended_at"] else (s.get("closed_reason") or "ended")
        rows.append(
            (str(s["id"]), s["project"], _short(s["title"], 28), _dt(s["started_at"]),
             _dt(s["ended_at"]) or "", state, _short(s.get("chat_id"), 12))
        )
    return cols, rows, entries


def _build_tasks(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("status", 6), ("due", 12), ("description", 52), ("created", 20)]
    entries = {}
    rows = []
    for t in db.list_tasks(conn, limit=500)["tasks"]:
        entries[str(t["id"])] = t
        rows.append(
            (str(t["id"]), t["project"], t["status"], t["due_date"] or "",
             _short(t["description"], 50), _dt(t["created_at"]))
        )
    return cols, rows, entries


def _build_decisions(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("title", 40), ("decision", 42), ("created", 20)]
    entries = {}
    rows = []
    for d in db.list_decisions(conn, limit=500)["decisions"]:
        entries[str(d["id"])] = d
        rows.append(
            (str(d["id"]), d["project"], _short(d["title"], 38),
             _short(d["decision"], 40), _dt(d["created_at"]))
        )
    return cols, rows, entries


def _build_snippets(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("lang", 10), ("description", 36), ("code", 40), ("created", 20)]
    entries = {}
    rows = []
    for s in db.find_snippets(conn, limit=500)["snippets"]:
        entries[str(s["id"])] = s
        rows.append(
            (str(s["id"]), s["project"], s["language"], _short(s["description"], 34),
             _short(s["code"], 38), _dt(s["created_at"]))
        )
    return cols, rows, entries


def _build_paths(conn) -> tuple[list[tuple[str, int]], list[tuple], dict]:
    cols = [("id", 5), ("project", 12), ("kind", 6), ("path", 44), ("description", 34), ("referenced", 20)]
    entries = {}
    rows = []
    for p in db.list_paths(conn, limit=500)["paths"]:
        entries[str(p["id"])] = p
        rows.append(
            (str(p["id"]), p["project"], p["kind"], _short(p["path"], 42),
             _short(p["description"], 32), _dt(p["last_referenced"]))
        )
    return cols, rows, entries


_BUILDERS = {
    "logs": _build_logs,
    "notes": _build_notes,
    "sessions": _build_sessions,
    "tasks": _build_tasks,
    "decisions": _build_decisions,
    "snippets": _build_snippets,
    "paths": _build_paths,
}


def _detail(kind: str, entry: dict) -> str:
    if kind == "log":
        session = f"session #{entry['session_id']}" if entry["session_id"] else "no session"
        tags = f"\ntags: {entry['tags']}" if entry["tags"] else ""
        return (
            f"Log #{entry['id']} · [{entry['category']}] · project {entry['project']}\n"
            f"{entry['timestamp']}  ({session}){tags}\n\n{entry['message']}"
        )
    if kind == "note":
        flags = [entry["priority"]] + (["pinned"] if entry["pinned"] else [])
        tags = f"\ntags: {entry['tags']}" if entry["tags"] else ""
        return (
            f"Note #{entry['id']} · {', '.join(flags)} · project {entry['project']}{tags}\n"
            f"created {entry['created_at']} · updated {entry['updated_at']}\n\n"
            f"{entry['title']}\n\n{entry['content']}"
        )
    if kind == "session":
        context = ""
        if entry.get("context"):
            try:
                context = "\n\nsaved context:\n" + json.dumps(json.loads(entry["context"]), indent=2)
            except (json.JSONDecodeError, TypeError):
                context = f"\n\nsaved context: {entry['context']}"
        state = "ACTIVE" if not entry["ended_at"] else (entry.get("closed_reason") or "ended")
        return (
            f"Session #{entry['id']} · project {entry['project']} · {state}"
            + (f" · chat {entry['chat_id']}" if entry.get("chat_id") else "")
            + f"\nstarted {entry['started_at']}"
            + (f" · ended {entry['ended_at']}" if entry["ended_at"] else "")
            + (f"\nsummary: {entry['summary']}" if entry["summary"] else "")
            + context
        )
    if kind == "task":
        return (
            f"Task #{entry['id']} · {entry['status']} · project {entry['project']}\n"
            f"created {entry['created_at']}"
            + (f" · due {entry['due_date']}" if entry["due_date"] else "")
            + (f" · completed {entry['completed_at']}" if entry["completed_at"] else "")
            + f"\n\n{entry['description']}"
        )
    if kind == "decision":
        parts = [f"Decision #{entry['id']} · project {entry['project']}", entry["title"], ""]
        parts.append(f"decision:     {entry['decision']}")
        if entry["rationale"]:
            parts.append(f"rationale:    {entry['rationale']}")
        if entry["alternatives"]:
            parts.append(f"alternatives: {entry['alternatives']}")
        parts.append(f"\nrecorded {entry['created_at']}")
        return "\n".join(parts)
    if kind == "snippet":
        tags = f"\ntags: {entry['tags']}" if entry["tags"] else ""
        return (
            f"Snippet #{entry['id']} · {entry['language']} · project {entry['project']}{tags}\n"
            f"{entry['description'] or ''}\n\n{entry['code']}"
        )
    if kind == "path":
        return (
            f"Path #{entry['id']} · {entry['kind']} · project {entry['project']}\n"
            f"{entry['path']}\n"
            f"{entry['description'] or ''}\n"
            f"last referenced {entry['last_referenced']}"
        )
    return str(entry)


_SEARCH_COLS = [("type", 9), ("id", 6), ("title", 42), ("project", 12)]


class LogbookUI(App):
    """Read-only viewer for the engineer logbook."""

    TITLE = "engineer-logbook"
    CSS = """
    #tabs { height: 1fr; }
    TabPane { layout: vertical; }
    DataTable { height: 1fr; width: 1fr; }
    #search-input { margin-bottom: 1; }
    #summary-text { padding: 1 2; }
    #detail {
        height: 30%;
        border: round #555;
        padding: 0 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("t", "today", "Today"),
        Binding("s", "focus_search", "Search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.conn = None
        self._maps: dict[str, dict[str, dict]] = {name: {} for name in list(_BUILDERS) + ["search"]}
        self._summary_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):
            for name, label in (
                ("logs", "Logs"), ("notes", "Notes"), ("sessions", "Sessions"),
                ("tasks", "Tasks"), ("decisions", "Decisions"),
                ("snippets", "Snippets"), ("paths", "Paths"),
            ):
                with TabPane(label, id=name):
                    yield DataTable(id=f"tbl-{name}", cursor_type="row")
            with TabPane("Summary", id="summary"):
                yield Static("Loading…", id="summary-text", markup=False)
            with TabPane("Search", id="search"):
                with Vertical():
                    yield Input(
                        placeholder="Search logs, notes, decisions, snippets — press Enter",
                        id="search-input",
                    )
                    yield DataTable(id="tbl-search", cursor_type="row")
        yield Static(
            "Highlight a row (↑/↓, Enter) to see full details here.\n"
            "Keys: r refresh · t today · s search · q quit",
            id="detail", markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.conn = db.connect()
        active = db.get_active_session(self.conn)
        location = db.describe()
        self.sub_title = (
            f"{location} · active session: #{active['id']} '{active['title']}'"
            if active else location
        )
        for name in list(_BUILDERS) + ["summary"]:
            self._populate(name)

    def on_unmount(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # ------------------------------------------------------------ loading

    def _populate(self, name: str) -> None:
        if name == "summary":
            summary = db.daily_summary(self.conn)["summary"]
            self._summary_text = summary["text"] + "\n\n(r refresh · t today)"
            self.query_one("#summary-text", Static).update(self._summary_text)
            return
        cols, rows, entries = _BUILDERS[name](self.conn)
        table = self.query_one(f"#tbl-{name}", DataTable)
        self._maps[name] = entries
        table.clear(columns=True)
        for label, width in cols:
            table.add_column(label, width=width)
        for row in rows:
            table.add_row(*row, key=row[0])

    def action_refresh(self) -> None:
        for name in list(_BUILDERS) + ["summary"]:
            self._populate(name)
        self.notify("Refreshed")

    def action_today(self) -> None:
        self.query_one(TabbedContent).active = "summary"
        self._populate("summary")

    def action_focus_search(self) -> None:
        self.query_one(TabbedContent).active = "search"
        self.query_one("#search-input", Input).focus()

    # ------------------------------------------------------------ events

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self._run_search(event.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_row(event.data_table.id, event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._show_row(event.data_table.id, event.row_key)

    def _show_row(self, table_id: str | None, row_key) -> None:
        if not table_id or row_key.value is None:
            return
        kind = table_id.removeprefix("tbl-")
        entry = self._maps.get(kind, {}).get(str(row_key.value))
        if entry is None:
            return
        if kind == "search":
            self.query_one("#detail", Static).update(
                f"[{entry['type']} #{entry['id']}] {entry['title']}\n\n{entry['content']}"
            )
        else:
            self.query_one("#detail", Static).update(_detail(kind, entry))

    def _run_search(self, query: str) -> None:
        table = self.query_one("#tbl-search", DataTable)
        self._maps["search"] = {}
        table.clear(columns=True)
        if not query.strip():
            return
        try:
            results = db.search(self.conn, query, limit=100)["results"]
        except ValueError as e:
            self.notify(str(e), severity="warning")
            return
        for label, width in _SEARCH_COLS:
            table.add_column(label, width=width)
        for r in results:
            key = f"{r['type']}:{r['id']}"
            self._maps["search"][key] = r
            table.add_row(r["type"], str(r["id"]), _short(r["title"], 40), r["project"], key=key)
        if not results:
            self.notify("No matches")


def main() -> None:
    """Run the logbook viewer TUI."""
    LogbookUI().run()


if __name__ == "__main__":
    main()
