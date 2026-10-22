"""Integration tests for the spec workflow engine (Postgres, logbookdb_test)."""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from engineer_logbook import db_pg, db_spec, spec_manifest, spec_render  # noqa: E402

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


@pytest.fixture()
def conn():
    c = db_pg.connect(DSN)
    yield c
    c.rollback()
    c.close()


# ---------------------------------------------------------------- manifest


def test_manifest_defines_three_processes():
    assert set(spec_manifest.process_ids()) == {"sdd", "bugfix", "assess"}


def test_next_step_skips_completed_and_orders_optional_last():
    completed = {"constitution", "specify"}
    assert spec_manifest.next_step_id("sdd", completed) == "plan"
    # optional clarify comes after all mandatory steps
    almost = {"constitution", "specify", "plan", "tasks", "implement", "converge"}
    assert spec_manifest.next_step_id("sdd", almost) == "clarify"


def test_next_step_none_when_all_done():
    everything = {s["id"] for s in spec_manifest.get_process("sdd")["steps"]}
    assert spec_manifest.next_step_id("sdd", everything) is None


# (pure rendering tests live in test_spec_render.py; these need the DB)


# ---------------------------------------------------------------- ops


def test_spec_start_and_progress(conn):
    out = db_spec.spec_start(conn, "sdd", project="proj-a", feature="user-auth")
    assert out["ok"] is True
    assert out["workflow"]["next_step"] == "constitution"
    assert out["workflow"]["nag"] and "constitution" in out["workflow"]["nag"]

    prog = db_spec.spec_progress(conn, project="proj-a")
    assert prog["ok"] is True
    assert prog["workflow"]["process"] == "sdd"
    assert prog["artifacts"] == []


def test_spec_start_replaces_active_workflow(conn):
    first = db_spec.spec_start(conn, "sdd", project="proj-a")
    _record(conn, "proj-a", "constitution.md", "constitution")
    second = db_spec.spec_start(conn, "bugfix", project="proj-a")
    assert second["replaced_workflow_id"] == first["workflow"]["id"]

    rows = conn.execute(
        "SELECT status FROM spec_workflows WHERE project = 'proj-a' ORDER BY id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["replaced", "active"]


def test_spec_step_delivery_and_prerequisite_gate(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    _record(conn, "proj-a", "constitution.md", "constitution")
    delivered = db_spec.spec_step(conn, "sdd", "specify", args="Build a widget", project="proj-a")
    assert "Build a widget" in delivered["prompt"]
    # delivering does not complete; cursor stays on the delivered step
    assert delivered["workflow"]["next_step"] == "specify"

    with pytest.raises(db_spec.SpecOpError, match="specify"):
        db_spec.spec_step(conn, "sdd", "plan", project="proj-a")

    db_spec.spec_report_artifact(conn, path="001-x/spec.md", content="# spec", step="specify", project="proj-a")
    db_spec.spec_complete_step(conn, "specify", project="proj-a")
    ok = db_spec.spec_step(conn, "sdd", "plan", project="proj-a")
    assert "research.md" in ok["prompt"]


def test_spec_step_auto_starts_workflow(conn):
    out = db_spec.spec_step(conn, "sdd", "constitution", project="fresh-proj")
    assert out["workflow"]["process"] == "sdd"
    assert out["workflow"]["status"] == "active"


def test_spec_step_wrong_process_resets_workflow(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    out = db_spec.spec_step(conn, "bugfix", "assess", args="crash", project="proj-a")
    assert out["workflow"]["process"] == "bugfix"


def test_artifact_snapshot_and_path_guard(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a", folder="docs/specs")
    out = db_spec.spec_report_artifact(
        conn, path="001-auth/spec.md", content="# spec", step="specify", project="proj-a"
    )
    assert out["artifact"]["path"] == "docs/specs/001-auth/spec.md"

    # same path refreshes instead of duplicating
    db_spec.spec_report_artifact(
        conn, path="docs/specs/001-auth/spec.md", content="# spec v2", step="specify",
        project="proj-a",
    )
    rows = conn.execute("SELECT content FROM spec_artifacts").fetchall()
    assert [r["content"] for r in rows] == ["# spec v2"]


def test_artifact_path_rejects_traversal_and_absolute(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    for bad in ("../escape.md", "/etc/passwd", "~/x.md", ""):
        with pytest.raises(db_spec.SpecOpError):
            db_spec.spec_report_artifact(
                conn, path=bad, content="x", step="specify", project="proj-a"
            )


def test_report_without_workflow_errors(conn):
    with pytest.raises(db_spec.SpecOpError, match="No active spec workflow"):
        db_spec.spec_report_artifact(conn, path="x.md", content="x", step="specify")


def test_artifacts_are_searchable(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    db_spec.spec_report_artifact(
        conn, path="001-photo/spec.md", content="zygomorphic photo organizer",
        step="specify", project="proj-a",
    )
    res = db_pg.search(conn, "zygomorphic")
    assert res["count"] == 1
    assert res["results"][0]["type"] == "spec_artifact"


def test_complete_and_finish_workflow(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    out = _record(conn, "proj-a", "constitution.md", "constitution")
    assert "constitution" in out["workflow"]["completed_steps"]
    assert out["workflow"]["next_step"] == "specify"

    _record(conn, "proj-a", "001-x/spec.md", "specify", "# spec")
    _record(conn, "proj-a", "001-x/plan.md", "plan", "# plan")
    _record(conn, "proj-a", "001-x/tasks.md", "tasks", "# tasks")
    _record(conn, "proj-a", "tests/unit/test_x.py", "implement", "# tests")
    _record(conn, "proj-a", "001-x/verification.md", "converge", "# all pass")

    fin = db_spec.spec_finish(conn, "sign-off: user approved", project="proj-a")
    assert fin["ok"] is True
    assert fin["workflow"]["status"] == "finished"
    assert fin["workflow"]["outcome"] == "sign-off: user approved"

    # finishing clears the nag
    assert db_spec.active_workflow_nag(conn, "proj-a") is None


def test_finish_without_workflow_errors(conn):
    with pytest.raises(db_spec.SpecOpError, match="No active spec workflow"):
        db_spec.spec_finish(conn, "done")


# ---------------------------------------------------------------- nags


def test_prior_context_includes_workflow_nag(conn):
    db_spec.spec_start(conn, "sdd", project="proj-a")
    _record(conn, "proj-a", "constitution.md", "constitution")
    db_spec.spec_report_artifact(conn, path="001-x/spec.md", content="# spec",
                                 step="specify", project="proj-a")
    db_spec.spec_complete_step(conn, "specify", project="proj-a")

    ctx = db_pg.start_session(conn, "continue work", project="proj-a", chat="chat-x")[
        "prior_context"
    ]
    assert "spec_workflow" in ctx
    assert "plan" in ctx["spec_workflow"]


def test_prior_context_no_nag_without_workflow(conn):
    ctx = db_pg.start_session(conn, "plain work", project="proj-b", chat="chat-y")[
        "prior_context"
    ]
    assert "spec_workflow" not in ctx


def test_daily_summary_contains_nag(conn):
    from engineer_logbook.db_pg import daily_summary

    db_spec.spec_start(conn, "bugfix", project="proj-c", slug="crash")
    text = daily_summary(conn, project="proj-c")["summary"]["text"]
    assert "Spec workflow:" in text
    assert "assess" in text

# ---------------------------------------------------------------- MCP-only context


def test_feature_dir_is_numbered_then_follows_the_recorded_spec(conn):
    db_spec.spec_start(conn, "sdd", project="numbered", feature="User Auth!")
    _record(conn, "numbered", "constitution.md", "constitution")
    first = db_spec.spec_step(conn, "sdd", "specify", args="auth", project="numbered")
    assert "Feature directory (FEATURE_DIR): `specs/001-user-auth`" in first["prompt"]

    # the agent picked a different directory name; later steps follow it
    db_spec.spec_report_artifact(conn, path="001-login/spec.md", content="# spec",
                                 step="specify", project="numbered")
    db_spec.spec_complete_step(conn, "specify", project="numbered")
    plan = db_spec.spec_step(conn, "sdd", "plan", project="numbered")
    open("/tmp/opencode/plan_prompt.txt", "w").write(plan["prompt"])
    assert "FEATURE_SPEC = `specs/001-login/spec.md`" in plan["prompt"]
    rec_line = next(l for l in plan["prompt"].splitlines() if l.startswith("- Artifacts already recorded:"))
    assert "`specs/001-login/spec.md`" in rec_line and "`specs/constitution.md`" in rec_line

    # the next feature in the same project gets the next number
    db_spec.spec_start(conn, "sdd", project="numbered", feature="Exports")
    nxt = db_spec.spec_step(conn, "sdd", "constitution", project="numbered")
    assert "Feature directory (FEATURE_DIR): `specs/002-exports`" in nxt["prompt"]


def test_custom_folder_flows_into_every_path(conn):
    db_spec.spec_start(conn, "sdd", project="docsfolder", feature="x", folder="docs/specs")
    out = db_spec.spec_step(conn, "sdd", "constitution", project="docsfolder")
    assert "Workflow folder: `docs/specs/`" in out["prompt"]
    assert "`docs/specs/constitution.md`" in out["prompt"]


def test_bugfix_chain_runs_end_to_end_through_the_db(conn):
    """Regression: fix/test were permanently blocked by file prerequisites."""
    out = db_spec.spec_step(conn, "bugfix", "assess", args="Login crashes slug=login-crash",
                            project="bugs")
    assert "Feature directory (FEATURE_DIR): `specs/bugs/login-crash`" in out["prompt"]
    with pytest.raises(db_spec.SpecOpError, match="'assess'"):
        db_spec.spec_step(conn, "bugfix", "fix", args="slug=login-crash", project="bugs")
    db_spec.spec_complete_step(conn, "assess", project="bugs")
    fix = db_spec.spec_step(conn, "bugfix", "fix", project="bugs")  # slug remembered
    assert "Slug: `login-crash`" in fix["prompt"]
    db_spec.spec_complete_step(conn, "fix", project="bugs")
    test = db_spec.spec_step(conn, "bugfix", "test", project="bugs")
    assert test["ok"] is True
    assert test["workflow"]["next_step"] == "test"


def test_spec_template_op():
    out = db_spec.spec_template("Plan")
    assert out["template"] == "plan" and out["file"] == "plan-template.md"
    assert "__SPECKIT" not in out["body"]
    with pytest.raises(db_spec.SpecOpError, match="unknown template"):
        db_spec.spec_template("nope")


# ---------------------------------------------------------------- evidence gates


def _record(conn, project, path, step, content="# x"):
    """Record an artifact and complete its step; returns the completion result."""
    db_spec.spec_report_artifact(conn, path=path, content=content, step=step, project=project)
    return db_spec.spec_complete_step(conn, step, project=project)


def _run_to_implement(conn):
    db_spec.spec_start(conn, "sdd", project="gate", feature="Tetris")
    _record(conn, "gate", "constitution.md", "constitution", "# principles")
    db_spec.spec_report_artifact(conn, path="001-tetris/spec.md", content="# spec",
                                 step="specify", project="gate")
    db_spec.spec_complete_step(conn, "specify", project="gate")
    db_spec.spec_report_artifact(conn, path="001-tetris/plan.md", content="# plan",
                                 step="plan", project="gate")
    db_spec.spec_complete_step(conn, "plan", project="gate")
    db_spec.spec_report_artifact(conn, path="001-tetris/tasks.md", content="# tasks",
                                 step="tasks", project="gate")
    db_spec.spec_complete_step(conn, "tasks", project="gate")


def test_implement_gate_needs_test_evidence(conn):
    _run_to_implement(conn)
    # no artifacts beyond tasks: gate refuses completion (needs 2 incl. a test file)
    with pytest.raises(db_spec.SpecOpError, match="not done"):
        db_spec.spec_complete_step(conn, "implement", project="gate")
    with pytest.raises(db_spec.SpecOpError, match=r"is not done yet"):
        db_spec.spec_complete_step(conn, "implement", project="gate")  # code only
    # code alone is insufficient: one non-test file still fails
    db_spec.spec_report_artifact(conn, path="src/tetris/board.py", content="...",
                                 step="implement", project="gate")
    with pytest.raises(db_spec.SpecOpError, match=r"not done yet"):
        db_spec.spec_complete_step(conn, "implement", project="gate")
    # tests recorded -> passes
    db_spec.spec_report_artifact(conn, path="tests/unit/test_board.py", content="...",
                                 step="implement", project="gate")
    out = db_spec.spec_complete_step(conn, "implement", project="gate")
    assert out["workflow"]["next_step"] == "converge"


def test_converge_gate_needs_verification_evidence(conn):
    _run_to_implement(conn)
    db_spec.spec_report_artifact(conn, path="tests/unit/test_board.py", content="...",
                                 step="implement", project="gate")
    db_spec.spec_complete_step(conn, "implement", project="gate")
    with pytest.raises(db_spec.SpecOpError, match=r"not done yet"):
        db_spec.spec_complete_step(conn, "converge", project="gate")
    db_spec.spec_report_artifact(conn, path="001-tetris/verification.md",
                                 content="# all criteria pass", step="converge", project="gate")
    out = db_spec.spec_complete_step(conn, "converge", project="gate")
    # mini-V loop: the next *delivery* of the loop is implement again
    # (cursor itself may move to optional steps like clarify)
    loop = db_spec.spec_step(conn, "sdd", "implement", project="gate")
    assert loop["ok"] is True
    assert "unit tests" in loop["prompt"]


def test_finish_requires_sign_off_and_completed_steps(conn):
    _run_to_implement(conn)
    with pytest.raises(db_spec.SpecOpError, match="incomplete steps"):
        db_spec.spec_finish(conn, "sign-off: approved", project="gate")
    with pytest.raises(db_spec.SpecOpError, match="sign-off"):
        db_spec.spec_finish(conn, "Converged", project="gate")
    db_spec.spec_report_artifact(conn, path="tests/unit/test_board.py", content="...",
                                 step="implement", project="gate")
    db_spec.spec_complete_step(conn, "implement", project="gate")
    # converge not completed yet
    with pytest.raises(db_spec.SpecOpError, match="incomplete steps"):
        db_spec.spec_finish(conn, "sign-off: approved", project="gate")
    db_spec.spec_report_artifact(conn, path="001-tetris/verification.md", content="ok",
                                 step="converge", project="gate")
    db_spec.spec_complete_step(conn, "converge", project="gate")
    out = db_spec.spec_finish(conn, "sign-off: user approved tetris v1 on 2026-10-09",
                              project="gate")
    assert out["ok"] is True and out["workflow"]["outcome"].startswith("sign-off")


def test_nag_teaches_mini_v_and_sign_off(conn):
    db_spec.spec_start(conn, "sdd", project="nagv", feature="Tetris")
    nag = db_spec.spec_progress(conn, project="nagv")["workflow"]["nag"]
    assert "constitution" in nag
    _record(conn, "nagv", "constitution.md", "constitution")
    db_spec.spec_report_artifact(conn, path="001-t/spec.md", content="# s", step="specify", project="nagv")
    db_spec.spec_complete_step(conn, "specify", project="nagv")
    db_spec.spec_report_artifact(conn, path="001-t/plan.md", content="# p", step="plan", project="nagv")
    db_spec.spec_complete_step(conn, "plan", project="nagv")
    db_spec.spec_report_artifact(conn, path="001-t/tasks.md", content="# t", step="tasks", project="nagv")
    out = db_spec.spec_complete_step(conn, "tasks", project="nagv")
    assert "mini-V" in out["workflow"]["nag"] and "unit tests" in out["workflow"]["nag"]
    db_spec.spec_report_artifact(conn, path="tests/test_a.py", content="x", step="implement", project="nagv")
    db_spec.spec_report_artifact(conn, path="tests/test_b.py", content="x", step="implement", project="nagv")
    db_spec.spec_complete_step(conn, "implement", project="nagv")
    db_spec.spec_report_artifact(conn, path="001-t/verification.md", content="v", step="converge", project="nagv")
    fin = db_spec.spec_complete_step(conn, "converge", project="nagv")
    assert "sign-off" in fin["workflow"]["nag"] or "spec_finish" in fin["workflow"]["nag"]


def test_render_context_mentions_the_gate(conn):
    db_spec.spec_start(conn, "sdd", project="rctx", feature="Tetris")
    _record(conn, "rctx", "constitution.md", "constitution")
    db_spec.spec_report_artifact(conn, path="001-x/spec.md", content="#", step="specify", project="rctx")
    db_spec.spec_complete_step(conn, "specify", project="rctx")
    db_spec.spec_report_artifact(conn, path="001-x/plan.md", content="#", step="plan", project="rctx")
    db_spec.spec_complete_step(conn, "plan", project="rctx")
    db_spec.spec_report_artifact(conn, path="001-x/tasks.md", content="#", step="tasks", project="rctx")
    out = db_spec.spec_complete_step(conn, "tasks", project="rctx")
    p = db_spec.spec_step(conn, "sdd", "implement", project="rctx")["prompt"]
    assert "CANNOT be completed until 2 file(s)" in p and "*test*" in p


def test_tool_descriptions_carry_the_rules():
    from engineer_logbook.server import mcp
    desc = {t.name: t.description for t in mcp._tool_manager.list_tools()}
    assert "REQUIRED before building" in desc["spec_start"]
    assert "Never write code before" in desc["spec_start"]
    assert "WITH unit tests" in desc["spec_step"] and "converge" in desc["spec_step"]
    assert "EVIDENCE-BASED" in desc["spec_complete_step"]
    assert "verification" in desc["spec_complete_step"]
    assert "SIGN-OFF" in desc["spec_finish"]
    assert "REQUIRED for every file" in desc["spec_report_artifact"]


def test_implement_prompt_teaches_mini_v(conn):
    db_spec.spec_start(conn, "sdd", project="mini", feature="T")
    _record(conn, "mini", "constitution.md", "constitution")
    db_spec.spec_report_artifact(conn, path="001-m/spec.md", content="#", step="specify", project="mini")
    db_spec.spec_complete_step(conn, "specify", project="mini")
    db_spec.spec_report_artifact(conn, path="001-m/plan.md", content="#", step="plan", project="mini")
    db_spec.spec_complete_step(conn, "plan", project="mini")
    db_spec.spec_report_artifact(conn, path="001-m/tasks.md", content="#", step="tasks", project="mini")
    db_spec.spec_complete_step(conn, "tasks", project="mini")
    p = db_spec.spec_step(conn, "sdd", "implement", project="mini")["prompt"]
    assert "mini-V" in p and "unit tests" in p
    c = db_spec.spec_step.__wrapped__ if hasattr(db_spec.spec_step, "__wrapped__") else None
    db_spec.spec_report_artifact(conn, path="tests/test_x.py", content="x", step="implement", project="mini")
    db_spec.spec_complete_step(conn, "implement", project="mini")
    cp = db_spec.spec_step(conn, "sdd", "converge", project="mini")["prompt"]
    assert "system" in cp.lower() and "sign off" in cp.lower() and "spec_finish" in cp
