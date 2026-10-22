"""Render spec workflow prompts for the logbook's MCP-only spec tools.

The step prompts (assets/commands) and templates (assets/templates) are
adapted from GitHub Spec Kit (MIT). Spec Kit relies on per-agent slash
commands, shell setup scripts, an extension-hook system and `.specify/`
state files; none of that exists here. Instead every rendered prompt opens
with a **Workflow context** block (paths, progress, recorded artifacts,
derived from the database by db_spec) and closes with the tool protocol for
recording results (spec_report_artifact, spec_complete_step).

Placeholders in the bundled markdown, filled at render time:
  {FOLDER}        workflow folder (default 'specs'); all artifacts live under it
  {FEATURE_DIR}   this workflow's directory, e.g. specs/003-user-auth,
                  specs/bugs/<slug>, specs/assessments/<slug>
  {CONSTITUTION}  project constitution, {FOLDER}/constitution.md
  __SPECKIT_COMMAND_X__  cross-reference to another step -> spec_step(...)

Pure functions over text: no database access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources

from . import spec_manifest

COMMANDS_PACKAGE = "engineer_logbook.assets.commands"
TEMPLATES_PACKAGE = "engineer_logbook.assets.templates"
TEMPLATE_FILES = {
    "spec": "spec-template.md",
    "plan": "plan-template.md",
    "tasks": "tasks-template.md",
    "constitution": "constitution-template.md",
    "checklist": "checklist-template.md",
}

_ARGUMENTS_BLOCK = re.compile(r"^```text\n\$ARGUMENTS\n```$", re.MULTILINE)
_SPECKIT_COMMAND = re.compile(r"__SPECKIT_COMMAND_([A-Z_]+)__")
_SLUG = re.compile(r"slug=([A-Za-z0-9][A-Za-z0-9_-]*)")
# per-process prefix of Spec Kit command tokens (BUG_FIX -> bugfix/fix)
_TOKEN_PREFIX = {"sdd": "", "bugfix": "BUG_", "assess": "ASSESS_"}


class SpecRenderError(Exception):
    """Raised when a step cannot be resolved or rendered."""


@dataclass(frozen=True)
class StepContext:
    """Workflow state the prompt needs, derived from the database."""

    project: str = "default"
    folder: str = "specs"
    feature_dir: str = "specs"
    constitution: str = "specs/constitution.md"
    slug: str = ""
    completed: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RenderedStep:
    prompt: str
    slug: str
    notes: tuple[str, ...]


def parse_slug(args: str) -> str:
    match = _SLUG.search(args or "")
    return match.group(1) if match else ""


def process_uses_slug(process_id: str) -> bool:
    """True when the process's steps operate on a slug (bugfix, assess)."""
    return bool(spec_manifest.slug_steps(process_id))


def check_prerequisites(process_id: str, step_id: str, completed_steps: set[str]) -> tuple[str, ...]:
    """Prerequisite step ids (from the manifest) not yet completed."""
    step = spec_manifest.get_step(process_id, step_id)
    return tuple(r for r in step.get("requires", []) if r not in completed_steps)


def _command_steps() -> dict[str, tuple[str, str]]:
    """Spec Kit command token -> (process, step), e.g. BUG_FIX -> (bugfix, fix)."""
    out: dict[str, tuple[str, str]] = {}
    for pid in spec_manifest.process_ids():
        prefix = _TOKEN_PREFIX.get(pid, f"{pid.upper()}_")
        for step in spec_manifest.get_process(pid)["steps"]:
            out[f"{prefix}{step['id'].upper()}"] = (pid, step["id"])
    return out


def substitute_command_references(text: str) -> str:
    """Rewrite __SPECKIT_COMMAND_X__ into the logbook tool call for that step."""
    steps = _command_steps()

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in steps:
            pid, sid = steps[token]
            return f"the `{sid}` step (`spec_step(\"{pid}\", \"{sid}\")`)"
        return f"the `{token.lower()}` step"

    return _SPECKIT_COMMAND.sub(_replace, text)


def _fill(text: str, ctx: StepContext) -> str:
    return (
        text.replace("{FEATURE_DIR}", ctx.feature_dir)
        .replace("{CONSTITUTION}", ctx.constitution)
        .replace("{FOLDER}", ctx.folder)
    )


def _substitute_arguments(text: str, args: str) -> str:
    supplied = args.strip() if args else ""
    block = supplied or (
        "(no input supplied: use the conversation so far, or ask the user if the "
        "step needs a description)"
    )
    text = _ARGUMENTS_BLOCK.sub(lambda _: f"```text\n{block}\n```", text, count=1)
    return text.replace("$ARGUMENTS", "the User Input")


def _load(package: str, file: str) -> str:
    try:
        raw = resources.files(package).joinpath(file).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SpecRenderError(f"bundled file '{file}' not found") from exc
    body = raw.strip()
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2].strip()
    return body


def _context_block(process_id: str, step_id: str, ctx: StepContext) -> str:
    step = spec_manifest.get_step(process_id, step_id)
    lines = [
        "## Workflow context (engineer-logbook)",
        "",
        "This step runs inside the engineer-logbook spec workflow. Everything you need "
        "comes from this prompt, the repository, and the logbook's MCP tools.",
        "",
        f"- Process / step: `{process_id}` / `{step_id}`"
        + (f" (completed: {', '.join(ctx.completed)})" if ctx.completed else " (no steps completed yet)"),
        f"- Workflow folder: `{ctx.folder}/`, every artifact of this workflow lives under it",
        f"- Feature directory (FEATURE_DIR): `{ctx.feature_dir}`",
        f"- Project constitution: `{ctx.constitution}` (project-wide; may not exist yet)",
    ]
    if ctx.slug:
        lines.append(f"- Slug: `{ctx.slug}`")
    if ctx.artifacts:
        lines.append("- Artifacts already recorded: " + ", ".join(f"`{a}`" for a in ctx.artifacts))
    else:
        lines.append("- Artifacts already recorded: none")
    gate = step.get("requires_artifacts")
    if gate:
        wants = " or ".join(f"`{g}`" for g in gate.get("any_of_globs", []))
        lines.append(
            f"- This step CANNOT be completed until {gate.get('min_total', 1)} file(s) are "
            f"recorded with spec_report_artifact"
            + (f" including {wants}" if wants else "")
            + f"; spec_complete_step('{step_id}') will refuse until then."
        )
    lines.append(
        "- Templates: call `spec_template` with name `spec`, `plan`, `tasks`, "
        "`constitution`, or `checklist`"
    )
    return "\n".join(lines)


_REC_BLOCK = """## Recording your work (required)

1. Write this step's files in the repository under `{FEATURE_DIR}` (the constitution goes to `{CONSTITUTION}`).
2. For every file you create or change, call `spec_report_artifact` with `path` (the file path), `content` (the full file text), `step="{STEP}"` and `project="{PROJECT}"`.
3. When the step's goals are met, call `spec_complete_step` with `step="{STEP}"` and `project="{PROJECT}"`. It checks the required artifacts, then returns the next step.

If the step stops early (unresolved questions, an `invalid` verdict, a failed gate), do not complete it: report why to the user instead.
"""

_MINI_V = """## This is one half of a mini-V

This workflow is a V-model in miniature. Each iteration has exactly two steps:

- **implement** (this step): write the code for the next unchecked task(s) AND its unit tests, run the tests until they pass.
- **converge**: verify the increment against the spec from the outside (system level), and reconcile the spec.

Integration and end-to-end testing across the whole system are intentionally out of scope for now, unless the spec explicitly demands them as acceptance criteria. Do not mark work done because the code exists: it is done when the tests for it are recorded and green.
"""


def _recording_block(process_id: str, step_id: str, ctx: StepContext) -> str:
    block = _REC_BLOCK
    block = (
        block.replace("{FEATURE_DIR}", ctx.feature_dir)
        .replace("{CONSTITUTION}", ctx.constitution)
        .replace("{STEP}", step_id)
        .replace("{PROJECT}", ctx.project)
    )
    out = [block]
    if step_id == "implement":
        out.append(
            _MINI_V
            + "\nFollow the repo's existing source/test layout for where code and tests "
            "go (create one under the repo root if it has none). Code and test files "
            "are artifacts: report each with `spec_report_artifact` (step=\"implement\")."
        )
    if step_id == "converge":
        out.append(
            "## Converge: verify, then either iterate or sign off\n\n"
            "1. Verify the increment against the spec from the OUTSIDE: exercise the "
            "running system (unit test failures from the implement step, runnable demos, "
            "or the verification you can drive without new code). Write what you ran, "
            "what the spec says, pass/fail per criterion into "
            f"`{ctx.feature_dir}/verification.md`, and record it with `spec_report_artifact`.\n"
            "2. Failures go back to `spec_step(\"" + process_id + "\", \"implement\")` as the next mini-V "
            "iteration (fix code or tests, add missed unit tests). Do not edit the spec to "
            "match the code; changing the spec is a user decision.\n"
            "3. When every spec criterion passes, STOP and ask the user to review the "
            "verification report and sign off. Do not call `spec_finish` yourself without "
            "their sign-off; on approval call `spec_finish` with "
            "outcome='sign-off: <what the user approved>'.\n"
            "4. Repeat converged reviews: implement → converge, until the user says the "
            "workflow is done — each review is a mini-V on the shrinking remainder."
            + ("" if ctx.slug else "")
            + ("\n\nSlug for artifact paths: " + ctx.slug if ctx.slug else "")
        )
    return "\n".join(out)


def render_step_prompt(
    process_id: str,
    step_id: str,
    args: str = "",
    context: StepContext | None = None,
) -> RenderedStep:
    """Render one step's prompt for an agent to execute.

    Raises SpecRenderError for an unknown process/step, a missing slug, or
    unmet prerequisite steps.
    """
    ctx = context or StepContext()
    try:
        step = spec_manifest.get_step(process_id, step_id)
    except spec_manifest.ManifestError as exc:
        raise SpecRenderError(str(exc)) from exc

    slug = parse_slug(args) or ctx.slug
    if process_uses_slug(process_id):
        first = spec_manifest.slug_steps(process_id)[0]
        if step_id != first and not slug:
            raise SpecRenderError(
                f"process '{process_id}' step '{step_id}' requires a slug. "
                "Pass 'slug=<name>' in args (e.g. 'slug=login-crash')."
            )

    unmet = check_prerequisites(process_id, step_id, set(ctx.completed))
    if unmet:
        raise SpecRenderError(
            "Step prerequisites not met. Complete these steps first: "
            + ", ".join(f"'{s}'" for s in unmet)
        )

    body = _load(COMMANDS_PACKAGE, step["file"])
    body = substitute_command_references(body)
    body = _substitute_arguments(body, args)
    prompt = "\n\n".join([
        _context_block(process_id, step_id, ctx),
        _fill(body, ctx),
        _recording_block(process_id, step_id, ctx),
    ])
    notes: list[str] = []
    if not (args or "").strip():
        notes.append("No input supplied for this step.")
    return RenderedStep(prompt=prompt, slug=slug, notes=tuple(notes))


def render_template(name: str) -> tuple[str, str]:
    """Return (file name, body) of a bundled artifact template."""
    file = TEMPLATE_FILES.get((name or "").strip().lower())
    if file is None:
        raise SpecRenderError(
            f"unknown template '{name}' (known: {', '.join(sorted(TEMPLATE_FILES))})"
        )
    return file, substitute_command_references(_load(TEMPLATES_PACKAGE, file))
