"""Tests for the Textual UI (read-only viewer), against Postgres."""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from engineer_logbook import db  # noqa: E402
from engineer_logbook import db_pg  # noqa: E402

from conftest import wipe  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


@pytest.fixture()
def seeded_db(monkeypatch):
    monkeypatch.setenv("ENGINEER_LOGBOOK_PG_DSN", DSN)
    conn = db_pg.connect(DSN)
    wipe(conn)
    conn.commit()
    db.start_session(conn, "UI test session", project="ui")
    db.add_log(conn, "docker networking bug", category="bug", tags=["docker"])
    db.add_log(conn, "added ui tab", category="fix")
    db.add_note(conn, "Note title", "note body text", tags=["core"])
    db.add_task(conn, "finish the ui", project="ui")
    db.add_decision(conn, "Textual", "use textual", rationale="tui fit")
    db.add_snippet(conn, "docker compose up", language="bash", description="start stack")
    db.remember_path(conn, "/repos/logbook", kind="repo", description="this repo")
    conn.commit()
    conn.close()
    return DSN


def test_ui_tabs_and_search(seeded_db):
    from textual.widgets import DataTable, TabbedContent

    from engineer_logbook.ui import LogbookUI

    async def run():
        app = LogbookUI()
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()

            expected = {"logs": 2, "notes": 1, "sessions": 1, "tasks": 1,
                        "decisions": 1, "snippets": 1, "paths": 1}
            tabs = app.query_one(TabbedContent)
            for name, count in expected.items():
                tabs.active = name
                await pilot.pause()
                table = app.query_one(f"#tbl-{name}", DataTable)
                assert table.row_count == count, f"tab {name}: {table.row_count} != {count}"

            # search via the internal runner (same path as the Input handler)
            tabs.active = "search"
            await pilot.pause()
            app._run_search("docker")
            await pilot.pause()
            results = app.query_one("#tbl-search", DataTable)
            assert results.row_count >= 1
            # a matching entry is registered for the detail pane
            assert app._maps["search"]

            # no-match search leaves the table empty
            app._run_search("quantumfluff")
            await pilot.pause()
            assert app.query_one("#tbl-search", DataTable).row_count == 0

            # summary tab populated
            tabs.active = "summary"
            app._populate("summary")
            await pilot.pause()
            assert "Logbook summary" in app._summary_text
            assert "docker networking bug" in app._summary_text

            # refresh does not crash and keeps data
            app.action_refresh()
            await pilot.pause()
            assert app.query_one("#tbl-logs", DataTable).row_count == 2

    import asyncio

    asyncio.run(run())


def test_detail_renderers():
    from engineer_logbook.ui import _detail

    log = {"id": 5, "category": "bug", "project": "api", "timestamp": "2026-09-15T10:00:00+00:00",
           "session_id": 2, "tags": "docker", "message": "compose networking broken"}
    text = _detail("log", log)
    assert "Log #5" in text and "[bug]" in text and "compose networking broken" in text

    decision = {"id": 1, "project": "api", "title": "Postgres", "decision": "use pg",
                "rationale": "pooled", "alternatives": None, "created_at": "2026-09-15T10:00:00+00:00"}
    text = _detail("decision", decision)
    assert "rationale:    pooled" in text and "alternatives" not in text.split("decision")[0]