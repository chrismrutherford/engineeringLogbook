"""Spec workflow prompt rendering for the MCP-only system (no database)."""

from __future__ import annotations

import re

import pytest

from engineer_logbook import spec_manifest, spec_render
from engineer_logbook.spec_render import StepContext, render_step_prompt, render_template

# Spec Kit plumbing that has no meaning in the MCP-only system.
PLUMBING = re.compile(
    r"\{SCRIPT\}|\{ARGS\}|\$ARGUMENTS|__SPECKIT_COMMAND|\.specify|extensions\.yml"
    r"|EXECUTE_COMMAND|[Hh]ook|scripts/|/memory/|preset|init-options|feature\.json"
    r"|GIT_BRANCH|speckit|Spec Kit|\{FEATURE_DIR\}|\{CONSTITUTION\}|\{FOLDER\}"
)

ALL_STEPS = [
    (pid, s["id"]) for pid in spec_manifest.process_ids()
    for s in spec_manifest.get_process(pid)["steps"]
]


def _ctx(pid: str, **kw) -> StepContext:
    steps = tuple(s["id"] for s in spec_manifest.get_process(pid)["steps"])
    base = dict(project="demo", folder="specs", feature_dir="specs/007-demo",
                constitution="specs/constitution.md", slug="s1", completed=steps)
    base.update(kw)
    return StepContext(**base)


@pytest.mark.parametrize("pid,sid", ALL_STEPS)
def test_every_step_renders_without_spec_kit_plumbing(pid, sid):
    prompt = render_step_prompt(pid, sid, args="do it slug=s1", context=_ctx(pid)).prompt
    hits = sorted({m.group(0) for m in PLUMBING.finditer(prompt)})
    assert hits == [], f"{pid}/{sid} still references: {hits}"
    assert prompt.startswith("## Workflow context (engineer-logbook)")
    assert "## Recording your work (required)" in prompt
    assert f'step="{sid}"' in prompt and 'project="demo"' in prompt


@pytest.mark.parametrize("name", sorted(spec_render.TEMPLATE_FILES))
def test_every_template_renders_without_plumbing(name):
    file, body = render_template(name)
    assert file == spec_render.TEMPLATE_FILES[name]
    assert body.strip()
    hits = sorted({m.group(0) for m in PLUMBING.finditer(body)})
    assert hits == [], f"template {name} still references: {hits}"


def test_unknown_template_is_rejected():
    with pytest.raises(spec_render.SpecRenderError, match="unknown template"):
        render_template("nope")


def test_paths_come_from_the_context():
    prompt = render_step_prompt(
        "sdd", "plan", args="Python CLI",
        context=_ctx("sdd", feature_dir="specs/003-user-auth",
                     artifacts=("specs/003-user-auth/spec.md",)),
    ).prompt
    assert "FEATURE_SPEC = `specs/003-user-auth/spec.md`" in prompt
    assert "IMPL_PLAN = `specs/003-user-auth/plan.md`" in prompt
    assert "`specs/constitution.md`" in prompt
    assert "Artifacts already recorded: `specs/003-user-auth/spec.md`" in prompt
    assert '`spec_template` tool (`name="plan"`)' in prompt


def test_user_input_is_substituted_and_empty_input_is_explained():
    prompt = render_step_prompt("sdd", "specify", args="Build a widget", context=_ctx("sdd")).prompt
    assert "```text\nBuild a widget\n```" in prompt
    empty = render_step_prompt("sdd", "specify", args="", context=_ctx("sdd"))
    assert "no input supplied" in empty.prompt
    assert empty.notes


def test_cross_references_become_tool_calls():
    prompt = render_step_prompt("bugfix", "test", args="slug=s1", context=_ctx("bugfix")).prompt
    assert 'spec_step("bugfix", "fix")' in prompt
    assert 'spec_step("bugfix", "assess")' in prompt


# ---------------------------------------------------------------- prerequisites


def test_plan_requires_specify():
    with pytest.raises(spec_render.SpecRenderError, match="'specify'"):
        render_step_prompt("sdd", "plan", context=_ctx("sdd", completed=("constitution",)))


@pytest.mark.parametrize("pid,sid,prior", [
    ("bugfix", "fix", "assess"),
    ("bugfix", "test", "fix"),
    ("assess", "research", "intake"),
    ("assess", "decide", "shape"),
])
def test_slug_processes_advance_once_the_prior_step_is_done(pid, sid, prior):
    """Regression: prerequisites used to be Spec Kit file paths the DB can't
    see, so these steps could never be delivered."""
    steps = [s["id"] for s in spec_manifest.get_process(pid)["steps"]]
    done = tuple(steps[: steps.index(prior) + 1])
    assert spec_render.check_prerequisites(pid, sid, set(done)) == ()
    render_step_prompt(pid, sid, args="slug=s1", context=_ctx(pid, completed=done))
    with pytest.raises(spec_render.SpecRenderError, match=f"'{prior}'"):
        render_step_prompt(pid, sid, args="slug=s1",
                           context=_ctx(pid, completed=tuple(s for s in done if s != prior)))


def test_slug_required_after_the_first_step():
    with pytest.raises(spec_render.SpecRenderError, match="slug"):
        render_step_prompt("bugfix", "fix", context=_ctx("bugfix", slug=""))


def test_unknown_step_raises():
    with pytest.raises(spec_render.SpecRenderError):
        render_step_prompt("sdd", "nope")


def test_manifest_rejects_unknown_prerequisite():
    bad = {"processes": {"p": {"steps": [
        {"id": "a", "file": "a.md", "requires": ["missing"]},
    ]}}}
    with pytest.raises(spec_manifest.ManifestError, match="unknown step 'missing'"):
        spec_manifest._validate(bad)
