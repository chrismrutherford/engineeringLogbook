"""End-to-end tests: run the real MCP server over stdio and talk to it
with the official mcp client, against a scratch Postgres database."""

import asyncio
import json
import os
import sys

import pytest

psycopg = pytest.importorskip("psycopg")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from engineer_logbook import db_pg  # noqa: E402

from conftest import wipe  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


def _params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "engineer_logbook.server"],
        env={**os.environ, "ENGINEER_LOGBOOK_PG_DSN": DSN},
    )


@pytest.fixture(autouse=True)
def _wipe_db():
    """Wipe the scratch database before each test."""
    c = db_pg.connect(DSN)
    wipe(c)
    c.commit()
    c.close()
    yield
    # teardown: also clean up any chat bindings left behind
    c = db_pg.connect(DSN)
    wipe(c)
    c.commit()
    c.close()


def _payload(res) -> dict:
    assert not res.isError, f"tool error: {res.content}"
    return json.loads(res.content[0].text)


def test_tools_listed():
    async def flow():
        async with stdio_client(_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                for expected in (
                    "start_session", "end_session", "get_current_session",
                    "save_session_context", "restore_session_context", "list_sessions",
                    "resume_session", "close_idle_sessions",
                    "add_log", "get_logs",
                    "add_note", "update_note", "list_notes", "pin_note", "delete_note",
                    "remember_path", "list_paths", "forget_path",
                    "add_task", "complete_task", "list_tasks",
                    "add_decision", "list_decisions",
                    "add_snippet", "find_snippets",
                    "spec_start", "spec_step", "spec_template", "spec_report_artifact",
                    "spec_complete_step", "spec_progress", "spec_finish",
                    "search", "daily_summary", "export_markdown", "stats",
                ):
                    assert expected in names, f"missing tool: {expected}"
                assert len(names) >= 36

                # idle state
                res = _payload(await session.call_tool("get_current_session", {}))
                assert res["ok"] is True and res["session"] is None

                # friendly error path
                res = _payload(await session.call_tool("end_session", {}))
                assert res["ok"] is False and "No active session" in res["error"]

    asyncio.run(flow())


def test_full_flow():
    async def flow():
        async with stdio_client(_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # session lifecycle
                res = _payload(await session.call_tool(
                    "start_session", {"title": "Auth refactor", "project": "api"}
                ))
                assert res["ok"] is True
                session_id = res["session"]["id"]

                res = _payload(await session.call_tool(
                    "start_session", {"title": "dup"}
                ))
                assert res["ok"] is False  # only one active session allowed

                # logs + context
                res = _payload(await session.call_tool(
                    "add_log",
                    {"message": "docker networking bug found", "category": "bug", "tags": ["docker"]},
                ))
                assert res["ok"] is True and res["log"]["project"] == "api"

                res = _payload(await session.call_tool(
                    "save_session_context",
                    {"context": {"branch": "feat/auth", "open_files": ["server.py"]}},
                ))
                assert res["ok"] is True and res["saved"]["session_id"] == session_id

                # notes, paths, tasks, decisions, snippets
                res = _payload(await session.call_tool(
                    "add_note", {"title": "Kafka notes", "content": "rebalancing gotcha", "priority": "high"}
                ))
                note_id = res["note"]["id"]

                res = _payload(await session.call_tool(
                    "remember_path", {"path": "/repos/api", "kind": "repo", "description": "main api"}
                ))
                assert res["ok"] is True

                res = _payload(await session.call_tool("add_task", {"description": "fix rebalancing"}))
                task_id = res["task"]["id"]

                res = _payload(await session.call_tool(
                    "add_decision", {"title": "Postgres", "decision": "use pg", "rationale": "pooled"}
                ))
                assert res["ok"] is True

                res = _payload(await session.call_tool(
                    "add_snippet", {"code": "docker compose up -d", "language": "bash", "description": "start stack"}
                ))
                assert res["ok"] is True

                # search across types
                res = _payload(await session.call_tool("search", {"query": "docker"}))
                assert {r["type"] for r in res["results"]} == {"log", "snippet"}

                # filters
                res = _payload(await session.call_tool("get_logs", {"category": "bug"}))
                assert res["count"] == 1

                res = _payload(await session.call_tool("list_paths", {"query": "api"}))
                assert res["count"] == 1

                res = _payload(await session.call_tool("complete_task", {"task_id": task_id}))
                assert res["task"]["status"] == "done"

                res = _payload(await session.call_tool("pin_note", {"note_id": note_id}))
                assert res["note"]["pinned"] == 1

                # reports
                res = _payload(await session.call_tool("daily_summary", {}))
                assert res["ok"] is True and "docker networking bug" in res["summary"]["text"]

                res = _payload(await session.call_tool("stats", {}))
                assert res["stats"]["counts"]["logs"] == 1

                res = _payload(await session.call_tool("export_markdown", {"project": "api"}))
                assert "# Engineer logbook" in res["markdown"]

                # end session, then restore from the latest session
                res = _payload(await session.call_tool("end_session", {"summary": "found it"}))
                assert res["session"]["ended_at"] is not None

                res = _payload(await session.call_tool("list_sessions", {}))
                assert res["sessions"][0]["active"] is False

                res = _payload(await session.call_tool("restore_session_context", {}))
                assert res["context"]["branch"] == "feat/auth"

    asyncio.run(flow())


def test_concurrent_chats():
    """Two MCP client connections share one DB but each owns its own session."""
    async def flow():
        async with stdio_client(_params()) as (r1, w1), stdio_client(_params()) as (r2, w2):
            async with ClientSession(r1, w1) as chatA, ClientSession(r2, w2) as chatB:
                await chatA.initialize()
                await chatB.initialize()

                # each chat starts its own session — no interference
                res_a = _payload(await chatA.call_tool(
                    "start_session", {"title": "chat A work", "project": "api"}))
                res_b = _payload(await chatB.call_tool(
                    "start_session", {"title": "chat B work", "project": "web"}))
                assert res_a["ok"] and res_b["ok"]
                id_a, id_b = res_a["session"]["id"], res_b["session"]["id"]
                assert id_a != id_b

                # cross-visibility: each chat sees only its own active session
                cur_a = _payload(await chatA.call_tool("get_current_session", {}))
                cur_b = _payload(await chatB.call_tool("get_current_session", {}))
                assert cur_a["session"]["id"] == id_a
                assert cur_b["session"]["id"] == id_b

                # logs land in the right session
                _payload(await chatA.call_tool("add_log", {"message": "A did stuff"}))
                _payload(await chatB.call_tool("add_log", {"message": "B did stuff"}))
                logs_a = _payload(await chatA.call_tool("get_logs", {"session_id": id_a}))
                assert [l["message"] for l in logs_a["logs"]] == ["A did stuff"]

                # ending A does not disturb B
                res = _payload(await chatA.call_tool("end_session", {"summary": "A done"}))
                assert res["session"]["ended_at"] is not None
                cur_b = _payload(await chatB.call_tool("get_current_session", {}))
                assert cur_b["session"]["id"] == id_b

    asyncio.run(flow())


def test_idle_close_and_resume():
    """An idle chat's session is swept by another chat's heartbeat, then the
    first chat reopens it with resume_session (context intact)."""
    async def flow():
        p = _params()

        async def new_chat():
            cm1 = stdio_client(p)
            read, write = await cm1.__aenter__()
            cm2 = ClientSession(read, write)
            await cm2.__aenter__()
            await cm2.initialize()
            return cm1, cm2

        cm1, chatA = await new_chat()
        try:
            _payload(await chatA.call_tool("start_session",
                                           {"title": "long task", "project": "api"}))
            res = _payload(await chatA.call_tool("save_session_context",
                                                 {"context": {"branch": "feat/z"}}))
            assert res["ok"]

            # simulate chat A going quiet: back-date its last heartbeat
            conn = db_pg.connect(DSN)
            conn.execute("UPDATE sessions SET last_activity_at = %s",
                         ("2026-01-01T00:00:00+00:00",))
            conn.commit()
            conn.close()

            # chat B wakes up; its heartbeat sweeps chat A's stale session
            cm3, chatB = await new_chat()
            try:
                cur_b = _payload(await chatB.call_tool("get_current_session", {}))
                assert cur_b["session"] is None  # B never started one
            finally:
                await chatB.__aexit__(None, None, None)
                await cm3.__aexit__(None, None, None)

            # chat A returns: its session was auto-closed
            cur_a = _payload(await chatA.call_tool("get_current_session", {}))
            assert cur_a["session"] is None

            # ...and is reopenable with context intact
            res = _payload(await chatA.call_tool("resume_session", {}))
            assert res["ok"] is True and res["reopened"] is True
            assert res["session"]["title"] == "long task"

            cur_a = _payload(await chatA.call_tool("get_current_session", {}))
            assert cur_a["session"]["title"] == "long task"

            ctx = _payload(await chatA.call_tool("restore_session_context", {}))
            assert ctx["context"] == {"branch": "feat/z"}
        finally:
            await chatA.__aexit__(None, None, None)
            await cm1.__aexit__(None, None, None)

    asyncio.run(flow())

def test_spec_workflow_end_to_end():
    """Full sdd flow over stdio: start, deliver, report artifact, complete, nag, finish."""

    async def flow():
        async with stdio_client(_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                res = _payload(await session.call_tool(
                    "spec_start", {"process": "sdd", "project": "specpro", "feature": "auth"}
                ))
                assert res["ok"] is True
                assert res["workflow"]["next_step"] == "constitution"

                # gate: plan before specify fails with ok: False
                res = _payload(await session.call_tool(
                    "spec_step", {"process": "sdd", "step": "plan", "project": "specpro"}
                ))
                assert res["ok"] is False and "specify" in res["error"]

                # deliver specify with args
                res = _payload(await session.call_tool(
                    "spec_step",
                    {"process": "sdd", "step": "specify", "args": "User auth", "project": "specpro"},
                ))
                assert res["ok"] is True
                assert "User auth" in res["prompt"]
                assert "$ARGUMENTS" not in res["prompt"]

                # evidence gate: constitution cannot complete without its file
                res = _payload(await session.call_tool(
                    "spec_complete_step", {"step": "constitution", "project": "specpro"}
                ))
                assert res["ok"] is False and "constitution.md" in res["error"]

                res = _payload(await session.call_tool(
                    "spec_report_artifact",
                    {"path": "constitution.md", "content": "# principles",
                     "step": "constitution", "project": "specpro"},
                ))
                res = _payload(await session.call_tool(
                    "spec_complete_step", {"step": "constitution", "project": "specpro"}
                ))
                assert res["workflow"]["next_step"] == "specify"

                res = _payload(await session.call_tool(
                    "spec_report_artifact",
                    {"path": "001-auth/spec.md", "content": "# spec: oauth2 login",
                     "step": "specify", "project": "specpro"},
                ))
                assert res["ok"] and res["artifact"]["path"] == "specs/001-auth/spec.md"

                res = _payload(await session.call_tool(
                    "spec_complete_step", {"step": "specify", "project": "specpro"}
                ))
                assert res["workflow"]["next_step"] == "plan"

                # progress shows artifacts + nag points at plan
                res = _payload(await session.call_tool("spec_progress", {"project": "specpro"}))
                art_paths = [a["path"] for a in res["artifacts"]]
                assert "specs/001-auth/spec.md" in art_paths
                assert "plan" in res["workflow"]["nag"]

                # snapshot is searchable
                res = _payload(await session.call_tool("search", {"query": "oauth2 login"}))
                assert res["count"] == 1
                assert res["results"][0]["type"] == "spec_artifact"

                # deliver plan with evidence; template fetchable; finish needs sign-off
                res = _payload(await session.call_tool(
                    "spec_step",
                    {"process": "sdd", "step": "plan", "project": "specpro", "args": "sqlite backend"},
                ))
                assert res["ok"] is True and 'name="plan"' in res["prompt"]
                res = _payload(await session.call_tool(
                    "spec_report_artifact",
                    {"path": "001-auth/plan.md", "content": "# plan", "step": "plan",
                     "project": "specpro"},
                ))
                res = _payload(await session.call_tool(
                    "spec_complete_step", {"step": "plan", "project": "specpro"}
                ))
                res = _payload(await session.call_tool(
                    "spec_finish", {"outcome": "Converged", "project": "specpro"}
                ))
                assert res["ok"] is False and "sign-off" in res["error"]

                for step, path, content in (
                    ("tasks", "001-auth/tasks.md", "# tasks"),
                    ("implement", "001-auth/src/game.py", "# code"),
                    ("implement", "001-auth/tests/test_game.py", "# unit tests"),
                    ("converge", "001-auth/verification.md", "# all criteria pass"),
                ):
                    res = _payload(await session.call_tool(
                        "spec_report_artifact",
                        {"path": path, "content": content, "step": step, "project": "specpro"},
                    ))
                    res = _payload(await session.call_tool(
                        "spec_complete_step", {"step": step, "project": "specpro"}
                    ))
                res = _payload(await session.call_tool(
                    "spec_finish",
                    {"outcome": "sign-off: user approved tetris v1", "project": "specpro"},
                ))
                assert res["ok"] is True and res["workflow"]["status"] == "finished"

                # no nag once finished
                res = _payload(await session.call_tool("spec_progress", {"project": "specpro"}))
                assert res["workflow"]["status"] == "finished"
                assert res["workflow"]["nag"] is None

    asyncio.run(flow())


def test_spec_workflow_nag_in_session_start():
    """start_session in a project with an active workflow nudges the next step."""

    async def flow():
        async with stdio_client(_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                _payload(await session.call_tool(
                    "spec_start", {"process": "bugfix", "project": "nagpro", "slug": "crash"}
                ))

                res = _payload(await session.call_tool(
                    "start_session", {"title": "fix the crash", "project": "nagpro"}
                ))
                ctx = res["prior_context"]
                assert "spec_workflow" in ctx
                assert "assess" in ctx["spec_workflow"]

    asyncio.run(flow())
