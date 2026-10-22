"""Tests for the engineer_logbook Postgres storage layer.

Gated on ENGINEER_LOGBOOK_PG_TEST_DSN — a DSN to a scratch database the
tests may freely wipe (logbookdb_test is meant for this).
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from engineer_logbook import db, db_pg  # noqa: E402

from conftest import wipe  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


@pytest.fixture()
def conn():
    c = db_pg.connect(DSN)
    # wipe all data between tests (children first)
    wipe(c)
    c.commit()
    yield c
    c.close()


def test_session_lifecycle(conn):
    s = db.start_session(conn, "Refactor auth", project="api")["session"]
    assert db.get_active_session(conn)["id"] == s["id"]

    with pytest.raises(ValueError):
        db.start_session(conn, "Another one")

    ended = db.end_session(conn, "all done")["session"]
    assert ended["ended_at"] is not None
    assert ended["summary"] == "all done"
    assert db.get_active_session(conn) is None

    with pytest.raises(ValueError):
        db.end_session(conn)


def test_session_context_roundtrip(conn):
    db.start_session(conn, "s1")
    db.save_session_context(conn, {"branch": "feat/x", "files": ["a.py"]})
    assert db.restore_session_context(conn)["context"] == {"branch": "feat/x", "files": ["a.py"]}

    db.end_session(conn)
    db.start_session(conn, "s2")
    # new session has no context yet
    assert db.restore_session_context(conn)["context"] is None
    db.end_session(conn)

    # no active session -> falls back to most recent session (s2, no context)
    assert db.restore_session_context(conn)["context"] is None
    # explicit session_id reaches back to s1
    out = db.restore_session_context(conn, session_id=1)
    assert out["context"]["branch"] == "feat/x"
    assert out["session"]["title"] == "s1"


def test_list_sessions(conn):
    db.start_session(conn, "one", project="api")
    db.end_session(conn)
    db.start_session(conn, "two", project="web")
    out = db.list_sessions(conn, project="api")["sessions"]
    assert [s["title"] for s in out] == ["one"]
    assert out[0]["active"] is False
    assert db.list_sessions(conn)["count"] == 2


# ------------------------------------------------- multi-chat sessions


def test_parallel_chats_have_independent_sessions(conn):
    s1 = db.start_session(conn, "chat A", project="api", chat="chatA")["session"]
    s2 = db.start_session(conn, "chat B", project="web", chat="chatB")["session"]
    assert s1["id"] != s2["id"]
    assert db.get_active_session(conn, chat="chatA")["id"] == s1["id"]
    assert db.get_active_session(conn, chat="chatB")["id"] == s2["id"]

    # ending chat A does not touch chat B
    db.end_session(conn, "done A", chat="chatA")
    assert db.get_active_session(conn, chat="chatA") is None
    assert db.get_active_session(conn, chat="chatB")["id"] == s2["id"]


def test_same_chat_cannot_start_two_sessions(conn):
    db.start_session(conn, "first", chat="chatA")
    with pytest.raises(ValueError):
        db.start_session(conn, "second", chat="chatA")


def test_logs_attach_to_chat_scoped_session(conn):
    db.start_session(conn, "A", project="api", chat="chatA")
    db.start_session(conn, "B", project="web", chat="chatB")
    db.add_log(conn, "from A", chat="chatA")
    db.add_log(conn, "from B", chat="chatB")
    logs_a = db.get_logs(conn, session_id=1)["logs"]
    logs_b = db.get_logs(conn, session_id=2)["logs"]
    assert [l["message"] for l in logs_a] == ["from A"]
    assert [l["message"] for l in logs_b] == ["from B"]


def test_resume_reopens_idle_closed_session(conn):
    s = db.start_session(conn, "long task", project="api", chat="chatA")["session"]
    db.save_session_context(conn, {"branch": "feat/x"}, chat="chatA")
    db.sweep_idle_sessions(conn, idle_minutes=0, include_chat="chatA")
    assert db.get_active_session(conn, chat="chatA") is None
    assert db.get_session(conn, s["id"])["closed_reason"] == "idle"

    out = db.resume_session(conn, chat="chatA")
    assert out["session"]["id"] == s["id"]
    assert out["session"]["ended_at"] is None
    assert out["session"]["closed_reason"] is None
    assert out["reopened"] is True
    assert db.get_active_session(conn, chat="chatA")["id"] == s["id"]

    ctx = db.restore_session_context(conn, chat="chatA")
    assert ctx["context"] == {"branch": "feat/x"}


def test_resume_most_recent_idle_session(conn):
    a = db.start_session(conn, "older", chat="chatA")["session"]
    db.sweep_idle_sessions(conn, idle_minutes=0, include_chat="chatA")
    b = db.start_session(conn, "newer", chat="chatA")["session"]
    db.sweep_idle_sessions(conn, idle_minutes=0, include_chat="chatA")

    out = db.resume_session(conn, chat="chatA")
    assert out["session"]["id"] == b["id"]


def test_resume_explicit_close_is_final(conn):
    s = db.start_session(conn, "wrapped up", chat="chatA")["session"]
    db.end_session(conn, "done for good", chat="chatA")
    with pytest.raises(ValueError, match="final"):
        db.resume_session(conn, session_id=s["id"], chat="chatA")


def test_resume_live_session_in_other_chat_rejected(conn):
    db.start_session(conn, "mine", chat="chatA")
    s2 = db.start_session(conn, "theirs", chat="chatB")["session"]
    with pytest.raises(ValueError, match="another chat"):
        db.resume_session(conn, session_id=s2["id"], chat="chatA")


def test_resume_orphaned_session_adopts_it(conn):
    # session left active by a chat whose process died (chatB never binds here)
    conn.execute(
        "INSERT INTO sessions(title, project, started_at, chat_id, last_activity_at)"
        " VALUES ('ghost', 'api', %s, 'chatB', %s)",
        (db.utcnow(), db.utcnow()),
    )
    ghost_id = conn.execute("SELECT id FROM sessions WHERE title='ghost'").fetchone()["id"]
    conn.commit()

    out = db.resume_session(conn, session_id=ghost_id, chat="chatA")
    assert out["adopted"] is True
    assert db.get_active_session(conn, chat="chatA")["id"] == ghost_id


def test_sweep_respects_idle_threshold(conn):
    s = db.start_session(conn, "fresh", chat="chatA")["session"]
    out = db.sweep_idle_sessions(conn, idle_minutes=60, include_chat="chatA")
    assert out["closed"] == 0
    assert db.get_active_session(conn, chat="chatA")["id"] == s["id"]

    out = db.sweep_idle_sessions(conn, idle_minutes=0, include_chat="chatA")
    assert out["closed"] == 1


def test_sweep_never_touches_legacy_sessions(conn):
    conn.execute(
        "INSERT INTO sessions(title, project, started_at) VALUES ('legacy', 'api', %s)",
        (db.utcnow(),),
    )
    conn.commit()
    out = db.sweep_idle_sessions(conn, idle_minutes=0)
    assert out["closed"] == 0
    assert db.get_active_session(conn) is not None


def test_sweep_clears_bindings(conn):
    s = db.start_session(conn, "x", chat="chatA")["session"]
    db.sweep_idle_sessions(conn, idle_minutes=0, include_chat="chatA")
    row = conn.execute("SELECT * FROM chat_bindings WHERE chat_id='chatA'").fetchone()
    assert row is None
    # chat A can start a new session after the sweep
    db.start_session(conn, "fresh start", chat="chatA")
    assert db.get_active_session(conn, chat="chatA")["id"] != s["id"]


def test_start_session_returns_prior_context(conn):
    db.start_session(conn, "Refactor auth flow", project="api")
    db.save_session_context(conn, {"branch": "feat/auth"})
    db.end_session(conn, "moved to JWTs")
    db.add_note(conn, "Auth gotcha", "skew < 30s", priority="high", project="api")
    db.add_task(conn, "finish oauth rotation", project="api")

    out = db.start_session(conn, "Continue auth refactor", project="api")
    pc = out["prior_context"]
    assert any("Refactor auth" in s["title"] for s in pc["recent_sessions_same_project"])
    assert any("auth" in s["title"].lower() for s in pc["related_sessions_by_title"])
    assert any("Auth gotcha" == n["title"] for n in pc["notes_pinned_or_priority"])
    assert any("oauth" in t["description"] for t in pc["open_tasks"])
    assert "resume_session" in pc["hint"]

    # unrelated project: empty but well-formed
    db.end_session(conn)
    out2 = db.start_session(conn, "Unrelated widgets", project="widgets")
    pc2 = out2["prior_context"]
    assert pc2["recent_sessions_same_project"] == []
    assert pc2["open_tasks"] == []
    assert "hint" in pc2


def test_add_and_query_logs(conn):
    db.start_session(conn, "s", project="api")
    db.add_log(conn, "bug in docker compose", category="bug", tags=["docker"])
    db.add_log(conn, "shipped the thing", category="fix", tags=["api"], project="web")

    logs = db.get_logs(conn, category="bug")["logs"]
    assert len(logs) == 1 and logs[0]["message"].startswith("bug")

    logs = db.get_logs(conn, tags=["docker"])["logs"]
    assert len(logs) == 1

    logs = db.get_logs(conn, project="web")["logs"]
    assert len(logs) == 1 and logs[0]["project"] == "web"

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert db.get_logs(conn, since=today, until=today)["count"] == 2

    # logs attach to the active session automatically
    assert all(l["session_id"] == 1 for l in db.get_logs(conn)["logs"])

    with pytest.raises(ValueError):
        db.get_logs(conn, since="not-a-date")

    with pytest.raises(ValueError):
        db.add_log(conn, "   ")


def test_notes_crud_and_pin(conn):
    n = db.add_note(conn, "Title", "body", priority="high", tags=["core"])["note"]
    with pytest.raises(ValueError):
        db.add_note(conn, "Bad", "x", priority="urgent")

    updated = db.update_note(conn, n["id"], content="new body", pinned=True)["note"]
    assert updated["content"] == "new body"
    assert updated["pinned"] == 1

    assert db.list_notes(conn, pinned_only=True)["count"] == 1
    assert db.list_notes(conn, tag="core")["count"] == 1
    assert db.list_notes(conn, tag="nope")["count"] == 0

    db.pin_note(conn, n["id"], pinned=False)
    assert db.list_notes(conn, pinned_only=True)["count"] == 0

    with pytest.raises(ValueError):
        db.update_note(conn, n["id"])

    assert db.delete_note(conn, n["id"])["deleted"] == n["id"]
    with pytest.raises(ValueError):
        db.update_note(conn, n["id"], title="ghost")


def test_paths_upsert_and_forget(conn):
    db.remember_path(conn, "/repos/api", kind="repo", description="main api")
    entry = db.remember_path(conn, "/repos/api", description="main api service")["path_entry"]
    assert entry["kind"] == "repo"
    assert entry["description"] == "main api service"
    assert db.list_paths(conn)["count"] == 1

    assert db.list_paths(conn, query="api")["count"] == 1
    assert db.list_paths(conn, query="zzz")["count"] == 0

    assert db.forget_path(conn, "/repos/api/")["deleted"]
    assert db.list_paths(conn)["count"] == 0
    with pytest.raises(ValueError):
        db.forget_path(conn, "/missing")


def test_tasks(conn):
    t = db.add_task(conn, "write tests", due_date="2026-09-20")["task"]
    with pytest.raises(ValueError):
        db.add_task(conn, "bad due date", due_date="2026-13-01")

    assert db.list_tasks(conn, status="open")["count"] == 1
    with pytest.raises(ValueError):
        db.list_tasks(conn, status="bogus")

    done = db.complete_task(conn, t["id"])["task"]
    assert done["status"] == "done" and done["completed_at"]
    # idempotent
    db.complete_task(conn, t["id"])
    assert db.list_tasks(conn, status="open")["count"] == 0
    assert db.list_tasks(conn, status="done")["count"] == 1


def test_decisions_and_snippets(conn):
    db.add_decision(conn, "Use Postgres", "pg for storage", rationale="zero deps", alternatives="flat files")
    assert db.list_decisions(conn)["count"] == 1
    with pytest.raises(ValueError):
        db.add_decision(conn, "", "")

    s = db.add_snippet(conn, "docker compose up -d", language="bash", description="start stack", tags=["docker"])["snippet"]
    assert s["language"] == "bash"
    assert db.find_snippets(conn)["count"] == 1
    assert db.find_snippets(conn, query="compose")["count"] == 1


def test_search_across_types(conn):
    db.add_log(conn, "docker networking bug", tags=["docker"])
    db.add_note(conn, "Kafka notes", "consumer rebalancing")
    db.add_decision(conn, "Use Postgres", "pg for storage")
    db.add_snippet(conn, "docker compose up -d", language="bash", description="start stack")

    res = db.search(conn, "docker")
    assert {r["type"] for r in res["results"]} == {"log", "snippet"}
    assert res["results"][0]["entry"] is not None

    res = db.search(conn, "kafka", types=["note"])
    assert res["count"] == 1 and res["results"][0]["type"] == "note"

    res = db.search(conn, "postgres")
    assert any(r["type"] == "decision" for r in res["results"])

    # no match
    assert db.search(conn, "quantum")["count"] == 0

    with pytest.raises(ValueError):
        db.search(conn, "x", types=["bogus"])


def test_daily_summary_and_stats_and_export(conn):
    from datetime import datetime, timezone

    db.start_session(conn, "s", project="api")
    db.add_log(conn, "did a thing", category="fix")
    db.add_note(conn, "N", "note body")
    db.add_decision(conn, "D", "choose x")
    db.add_task(conn, "t1")
    t = db.add_task(conn, "t2")["task"]
    db.complete_task(conn, t["id"])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s = db.daily_summary(conn, today)["summary"]
    assert s["logs_count"] == 1
    assert len(s["tasks_completed"]) == 1
    assert len(s["tasks_open"]) == 1
    assert "did a thing" in s["text"]
    with pytest.raises(ValueError):
        db.daily_summary(conn, "2026-02-30")

    st = db.stats(conn)["stats"]
    assert st["counts"]["logs"] == 1
    assert st["counts"]["tasks"] == 2
    assert len(st["activity_last_7_days"]) == 7
    assert st["active_session"]["title"] == "s"

    md = db.export_markdown(conn)["markdown"]
    assert "# Engineer logbook" in md
    assert "## Logs" in md and "did a thing" in md
    assert "## Tasks" in md and "- [x] t2" in md
    assert "note body" in md

    # project filter: api export has the session-scoped log but not default-project notes
    md_api = db.export_markdown(conn, project="api")["markdown"]
    assert "did a thing" in md_api and "#1 s" in md_api
    assert "note body" not in md_api

    with pytest.raises(ValueError):
        db.export_markdown(conn, since="oops")

def test_start_session_persists_when_best_effort_context_fails(conn, monkeypatch):
    """Regression: a failing best-effort query inside prior_context (e.g. the
    spec-workflow nag before its tables existed) aborted the transaction, so
    the final COMMIT silently rolled back the new session. The tool still
    returned an id, but nothing was stored."""
    from engineer_logbook import db_spec

    def broken_nag(c, project):
        c.execute("SELECT * FROM relation_that_does_not_exist")

    monkeypatch.setattr(db_spec, "active_workflow_nag", broken_nag)
    out = db.start_session(conn, "survives", project="nagfail", chat="chat-nagfail")
    conn.commit()
    assert "spec_workflow" not in out["prior_context"]

    other = db_pg.connect(DSN)  # independent view of what was committed
    try:
        row = other.execute(
            "SELECT title FROM sessions WHERE id = %s", (out["session"]["id"],)
        ).fetchone()
        assert row is not None and row["title"] == "survives"
        bound = other.execute(
            "SELECT session_id FROM chat_bindings WHERE chat_id = 'chat-nagfail'"
        ).fetchone()
        assert bound is not None and bound["session_id"] == out["session"]["id"]
    finally:
        other.close()


def test_daily_summary_survives_failing_nag(conn, monkeypatch):
    from engineer_logbook import db_spec

    def broken_nag(c, project):
        c.execute("SELECT * FROM relation_that_does_not_exist")

    monkeypatch.setattr(db_spec, "active_workflow_nag", broken_nag)
    db.add_log(conn, "still counted", project="nagfail")
    text = db.daily_summary(conn, project="nagfail")["summary"]["text"]
    assert "still counted" in text
    assert "Spec workflow:" not in text
    # the connection is still usable afterwards (transaction not aborted)
    conn.execute("SELECT 1").fetchone()
