"""Spec workflow DB operations: start, deliver steps, record artifacts, nags.

State model: one ACTIVE workflow per project. Artifacts are the agent's
on-disk deliverables under a project folder (default 'specs/'); the DB holds
progress events and content snapshots. No local state files.
"""

from __future__ import annotations

import os
import re
import psycopg

from . import spec_render, spec_manifest

DEFAULT_FOLDER = "specs"

_FOLDER_NOTE = (
    "artifacts_folder: the project folder the workflow's files live under "
    "(default 'specs'; e.g. pass 'docs/specs' to keep documents together)"
)


class SpecOpError(ValueError):
    """Expected spec-workflow failure (spec_start step gates, guards).

    Subclasses ValueError so it flows through the same {'ok': False,
    'error': ...} mapping the storage layer uses for expected errors.
    """


def _wf_d(row) -> dict | None:
    return dict(row) if row is not None else None


def get_active_workflow(conn: psycopg.Connection, project: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM spec_workflows WHERE project = %s AND status = 'active'"
        " ORDER BY id DESC LIMIT 1",
        (project or "default",),
    ).fetchone()
    return _wf_d(row)


def _completed_steps(conn: psycopg.Connection, workflow_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT step FROM spec_events"
        " WHERE workflow_id = %s AND action = 'complete'",
        (workflow_id,),
    ).fetchall()
    return {r["step"] for r in rows}


def _workflow_payload(conn: psycopg.Connection, wf: dict) -> dict:
    wf = dict(wf)
    wf["completed_steps"] = sorted(_completed_steps(conn, wf["id"]))
    nxt = spec_manifest.next_step_id(wf["process"], set(wf["completed_steps"]))
    wf["next_step"] = nxt
    # every mandatory step done -> sign-off nag beats the optional-step cursor
    mandatory_left = [
        s["id"] for s in spec_manifest.get_process(wf["process"])["steps"]
        if not s.get("optional") and s["id"] not in set(wf["completed_steps"])
    ]
    if not mandatory_left:
        wf["nag"] = _nag_text(wf, None)
    else:
        wf["nag"] = _nag_text(wf, nxt)
    return wf


def _nag_text(wf: dict, next_step: str | None) -> str | None:
    if wf["status"] != "active":
        return None
    if next_step is None:
        return (
            f"Spec workflow '{wf['process']}' ({wf['feature'] or wf['slug'] or wf['id']}) "
            "has all steps complete — get the user's sign-off, then call spec_finish "
            "with it (e.g. outcome='sign-off: <what the user approved>')."
        )
    phase = ""
    if wf["process"] == "sdd" and next_step in ("implement", "converge"):
        phase = (" Iterate in mini-Vs: implement writes code WITH unit tests, converge "
                 "verifies the increment against the spec.")
    return (
        f"Active spec workflow '{wf['process']}' "
        f"({wf['feature'] or wf['slug'] or wf['id']}): next step is '{next_step}' "
        f"— deliver it with spec_step('{wf['process']}', '{next_step}', ...), then "
        f"report artifacts with spec_report_artifact.{phase}"
    )


def spec_start(
    conn: psycopg.Connection,
    process: str,
    project: str = "default",
    feature: str | None = None,
    slug: str | None = None,
    folder: str | None = None,
) -> dict:
    """Start (or replace) the active spec workflow for a project."""
    try:
        spec_manifest.get_process(process)
    except spec_manifest.ManifestError as exc:
        raise SpecOpError(str(exc)) from exc

    project = project or "default"
    now = _now(conn)
    existing = get_active_workflow(conn, project)
    replaced = None
    if existing:
        replaced = existing["id"]
        conn.execute(
            "UPDATE spec_workflows SET status = 'replaced', updated_at = %s,"
            " finished_at = %s WHERE id = %s AND status = 'active'",
            (now, now, existing["id"]),
        )
    row = conn.execute(
        "INSERT INTO spec_workflows(process, project, feature, slug, folder,"
        " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (process, project, feature, slug, folder or DEFAULT_FOLDER, now, now),
    ).fetchone()
    return {
        "ok": True,
        "workflow": _workflow_payload(conn, _wf_d(row)),
        "replaced_workflow_id": replaced,
        "note": _FOLDER_NOTE,
    }


def _slugify(text: str, max_len: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:max_len].rstrip("-")


def _feature_dir(conn: psycopg.Connection, wf: dict, slug: str) -> str:
    """Where this workflow's artifacts go, always inside its folder."""
    folder = wf["folder"]
    if wf["process"] == "bugfix":
        return f"{folder}/bugs/{slug or '<slug>'}"
    if wf["process"] == "assess":
        return f"{folder}/assessments/{slug or '<slug>'}"
    # sdd: the directory spec.md was recorded in, else the project's next number
    row = conn.execute(
        "SELECT path FROM spec_artifacts WHERE workflow_id = %s AND path LIKE %s"
        " ORDER BY id DESC LIMIT 1",
        (wf["id"], "%/spec.md"),
    ).fetchone()
    if row:
        return row["path"].rsplit("/", 1)[0]
    used = conn.execute(
        "SELECT DISTINCT a.path FROM spec_artifacts a"
        " JOIN spec_workflows w ON w.id = a.workflow_id"
        " WHERE w.project = %s AND w.process = 'sdd' AND w.id <> %s AND a.path LIKE %s",
        (wf["project"], wf["id"], "%/spec.md"),
    ).fetchall()
    number = len({r["path"].rsplit("/", 1)[0] for r in used}) + 1
    short = _slugify(wf.get("feature") or "") or "<short-name>"
    return f"{folder}/{number:03d}-{short}"


def _step_context(conn: psycopg.Connection, wf: dict, args: str) -> spec_render.StepContext:
    slug = spec_render.parse_slug(args) or wf.get("slug") or ""
    artifacts = tuple(
        r["path"] for r in conn.execute(
            "SELECT path FROM spec_artifacts WHERE workflow_id = %s ORDER BY id", (wf["id"],)
        ).fetchall()
    )
    return spec_render.StepContext(
        project=wf["project"],
        folder=wf["folder"],
        feature_dir=_feature_dir(conn, wf, slug),
        constitution=f"{wf['folder']}/constitution.md",
        slug=slug,
        completed=tuple(
            s["id"] for s in spec_manifest.get_process(wf["process"])["steps"]
            if s["id"] in _completed_steps(conn, wf["id"])
        ),
        artifacts=artifacts,
    )


def spec_template(name: str) -> dict:
    """A bundled artifact template (spec, plan, tasks, constitution, checklist)."""
    try:
        file, body = spec_render.render_template(name)
    except spec_render.SpecRenderError as exc:
        raise SpecOpError(str(exc)) from exc
    return {"template": name.strip().lower(), "file": file, "body": body}


def spec_step(
    conn: psycopg.Connection,
    process: str,
    step: str,
    args: str = "",
    project: str = "default",
    folder: str | None = None,
) -> dict:
    """Deliver the rendered prompt for one step; marks it in progress.

    Enforces prerequisites against DB-tracked completions. Also auto-starts
    the active workflow for the project when none exists or its process
    differs. Raises SpecOpError for unknown steps or unmet prerequisites.
    """
    project = project or "default"
    wf = get_active_workflow(conn, project)
    if wf is None or wf["process"] != process:
        started = spec_start(conn, process=process, project=project, folder=folder)
        wf = started["workflow"]

    try:
        rendered = spec_render.render_step_prompt(
            process, step, args=args, context=_step_context(conn, wf, args)
        )
    except spec_render.SpecRenderError as exc:
        raise SpecOpError(str(exc)) from exc

    now = _now(conn)
    conn.execute(
        "INSERT INTO spec_events(workflow_id, step, action, detail, created_at)"
        " VALUES (%s, %s, 'deliver', %s, %s)",
        (wf["id"], step, (args or None), now),
    )
    conn.execute(
        "UPDATE spec_workflows SET updated_at = %s,"
        " slug = COALESCE(NULLIF(%s, ''), slug) WHERE id = %s",
        (now, rendered.slug, wf["id"]),
    )
    wf = get_active_workflow(conn, project)
    out = {"workflow": _workflow_payload(conn, _wf_d(wf))}
    out.update(
        {
            "ok": True,
            "step": step,
            "prompt": rendered.prompt,
            "notes": list(rendered.notes),
            "instruction": (
                "Execute this prompt now. When the step's artifacts are written, "
                "call spec_report_artifact for each file (path + content) so the "
                "logbook records them, then spec_complete_step to mark the step done."
            ),
        }
    )
    return out


def spec_report_artifact(
    conn: psycopg.Connection,
    path: str,
    content: str,
    step: str,
    project: str = "default",
) -> dict:
    """Record (or refresh) an artifact snapshot for the active workflow.

    The path must stay inside the workflow's folder: no absolute paths, no
    escaping the folder root.
    """
    wf = get_active_workflow(conn, project)
    if wf is None:
        raise SpecOpError(
            f"No active spec workflow for project '{project or 'default'}'; "
            "call spec_step (or spec_start) first."
        )

    clean = _safe_artifact_path(path, wf["folder"])
    now = _now(conn)
    conn.execute(
        "INSERT INTO spec_artifacts(workflow_id, step, path, kind, content, recorded_at)"
        " VALUES (%s, %s, %s, 'file', %s, %s)"
        " ON CONFLICT(workflow_id, path) DO UPDATE SET"
        " content = excluded.content, step = excluded.step, recorded_at = excluded.recorded_at",
        (wf["id"], step, clean, content, now),
    )
    conn.execute(
        "UPDATE spec_workflows SET updated_at = %s WHERE id = %s", (now, wf["id"])
    )
    conn.execute(
        "INSERT INTO spec_events(workflow_id, step, action, detail, created_at)"
        " VALUES (%s, %s, 'artifact', %s, %s)",
        (wf["id"], step, clean, now),
    )
    return {
        "ok": True,
        "workflow_id": wf["id"],
        "artifact": {"path": clean, "step": step, "bytes": len(content)},
    }


def _safe_artifact_path(path: str, folder: str) -> str:
    """Normalize and validate an artifact path against the workflow folder."""
    raw = (path or "").strip()
    if not raw:
        raise SpecOpError("artifact path is empty")
    if raw.startswith(("/", "~")) or ":" in raw.split("/")[0] or "\\" in raw:
        raise SpecOpError(f"artifact path must be relative to the workflow folder: {path!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise SpecOpError(f"artifact path must not traverse upward or be empty: {path!r}")

    folder_parts = [p for p in (folder or DEFAULT_FOLDER).strip("/").split("/") if p]
    if parts[: len(folder_parts)] == folder_parts:
        clean = "/".join(parts)
    else:
        clean = "/".join(folder_parts + parts)
    return clean


def _artifact_artifacts(conn: psycopg.Connection, workflow_id: int) -> list[str]:
    return [r["path"] for r in conn.execute(
        "SELECT path FROM spec_artifacts WHERE workflow_id = %s ORDER BY id", (workflow_id,)
    ).fetchall()]


def _step_artifact_gap(
    conn: psycopg.Connection, wf: dict, step: str
) -> tuple[str, ...]:
    """What a step's requires_artifacts gate still needs recorded."""
    gate = spec_manifest.get_step(wf["process"], step).get("requires_artifacts")
    if not gate:
        return ()
    globs = [g.replace("{FEATURE_DIR}/", "") for g in gate.get("any_of_globs", [])]
    paths = _artifact_artifacts(conn, wf["id"])
    scored = []
    for g in globs:
        rx = re.compile(
            "^"
            + ".*".join(re.escape(p) if p != "*" else ".*" for p in g.split("*"))
            + "$"
        )
        matched = [p for p in paths if rx.search(p.rsplit("/", 1)[-1])]
        scored.append((len(matched), g))
    total = len(paths)
    missing: list[str] = []
    if total < gate.get("min_total", 0):
        missing.append(
            f"at least {gate['min_total']} file(s) recorded with spec_report_artifact "
            f"(have {total})"
        )
    if scored and all(c == 0 for c, _ in scored):
        missing.append("a file matching: " + " or ".join(g for _, g in scored))
    return tuple(missing)


def spec_complete_step(
    conn: psycopg.Connection,
    step: str,
    project: str = "default",
) -> dict:
    """Mark a workflow step complete once its required artifacts are recorded.

    Refuses (SpecOpError) while the step's requires_artifacts gate is unmet —
    e.g. implement requires recorded test files — so completion is evidence,
    not self-report.
    """
    project = project or "default"
    wf = get_active_workflow(conn, project)
    if wf is None:
        raise SpecOpError(f"No active spec workflow for project '{project}'.")
    try:
        spec_manifest.get_step(wf["process"], step)
    except spec_manifest.ManifestError as exc:
        raise SpecOpError(str(exc)) from exc

    gap = _step_artifact_gap(conn, wf, step)
    if gap:
        raise SpecOpError(
            f"Step '{step}' is not done yet — record these first via "
            f"spec_report_artifact:\n  - " + "\n  - ".join(gap)
        )

    now = _now(conn)
    conn.execute(
        "INSERT INTO spec_events(workflow_id, step, action, created_at)"
        " VALUES (%s, %s, 'complete', %s)",
        (wf["id"], step, now),
    )
    conn.execute(
        "UPDATE spec_workflows SET updated_at = %s WHERE id = %s", (now, wf["id"])
    )
    wf = get_active_workflow(conn, project)
    out = {"workflow": _workflow_payload(conn, _wf_d(wf))}
    out["ok"] = True
    out["completed_step"] = step
    return out


def spec_progress(conn: psycopg.Connection, project: str = "default") -> dict:
    """State of the project's most recent workflow: steps, artifacts, next step."""
    wf = get_active_workflow(conn, project)
    if wf is None:
        wf = _wf_d(
            conn.execute(
                "SELECT * FROM spec_workflows WHERE project = %s ORDER BY id DESC LIMIT 1",
                (project or "default",),
            ).fetchone()
        )
    if wf is None:
        processes = spec_manifest.process_ids()
        return {
            "ok": True,
            "workflow": None,
            "message": (
                f"No spec workflow for project '{project or 'default'}'. "
                f"Known processes: {', '.join(processes)}."
            ),
        }
    wf = _workflow_payload(conn, wf)
    artifacts = [
        dict(r)
        for r in conn.execute(
            "SELECT step, path, recorded_at, length(content) AS bytes"
            " FROM spec_artifacts WHERE workflow_id = %s ORDER BY id",
            (wf["id"],),
        ).fetchall()
    ]
    events = [
        dict(r)
        for r in conn.execute(
            "SELECT step, action, detail, created_at FROM spec_events"
            " WHERE workflow_id = %s ORDER BY id DESC LIMIT 20",
            (wf["id"],),
        ).fetchall()
    ]
    return {"ok": True, "workflow": wf, "artifacts": artifacts, "recent_events": events}


def spec_finish(
    conn: psycopg.Connection,
    outcome: str,
    project: str = "default",
) -> dict:
    """Finish the active workflow; the outcome must record the user's sign-off.

    Refuses while steps are incomplete (finish is for converged workflows,
    not abandoned ones) and refuses an outcome with no sign-off statement.
    """
    wf = get_active_workflow(conn, project)
    if wf is None:
        raise SpecOpError(
            f"No active spec workflow for project '{project or 'default'}'."
        )
    text = (outcome or "").strip()
    if not text:
        raise SpecOpError("spec_finish requires an outcome describing the result.")
    low = text.lower()
    if not ("sign-off" in low or "signed off" in low):
        raise SpecOpError(
            "spec_finish must record the user's sign-off, e.g. "
            "outcome='sign-off: user approved <what> on <date>'. "
            "Ask the user to review the verification results and sign off first."
        )
    if wf["process"] == "sdd":
        incomplete = [
            s["id"] for s in spec_manifest.get_process("sdd")["steps"]
            if not s.get("optional") and s["id"] not in _completed_steps(conn, wf["id"])
        ]
        if incomplete:
            raise SpecOpError(
                "sdd workflow still has incomplete steps: "
                + ", ".join(f"'{s}'" for s in incomplete)
                + ". Iterate implement/converge until the spec passes."
            )
    now = _now(conn)
    conn.execute(
        "UPDATE spec_workflows SET status = 'finished', finished_at = %s,"
        " outcome = %s, updated_at = %s WHERE id = %s AND status = 'active'",
        (now, outcome, now, wf["id"]),
    )
    conn.execute(
        "INSERT INTO spec_events(workflow_id, step, action, detail, created_at)"
        " VALUES (%s, 'finish', 'finish', %s, %s)",
        (wf["id"], outcome, now),
    )
    return {
        "ok": True,
        "workflow": _wf_d(
            conn.execute(
                "SELECT * FROM spec_workflows WHERE id = %s", (wf["id"],)
            ).fetchone()
        ),
    }


def active_workflow_nag(conn: psycopg.Connection, project: str) -> str | None:
    """One-line nag for an active workflow, for injection into session context."""
    if not project or project == "default":
        return None
    wf = get_active_workflow(conn, project)
    if wf is None:
        return None
    completed = _completed_steps(conn, wf["id"])
    return _nag_text(_workflow_payload(conn, wf),
                     spec_manifest.next_step_id(wf["process"], completed))


def _now(conn: psycopg.Connection) -> str:
    return conn.execute("SELECT to_char(now() AT TIME ZONE 'UTC',"
                        " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t").fetchone()["t"]