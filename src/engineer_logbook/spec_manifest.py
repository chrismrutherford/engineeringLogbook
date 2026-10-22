"""Typed manifest for spec workflow processes, steps, and prerequisites.

Loaded from the bundled assets/processes.json (ported from spec-kit via
spec-mcp). Pure data: no filesystem state, no database access.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

MANIFEST_FILE = "processes.json"


class ManifestError(Exception):
    """Raised when the process manifest is missing or malformed."""


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """Load and validate the bundled process manifest."""
    try:
        raw = resources.files("engineer_logbook.assets").joinpath(MANIFEST_FILE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise ManifestError(f"bundled manifest {MANIFEST_FILE} not found") from exc

    import json

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {MANIFEST_FILE} is not valid JSON: {exc}") from exc

    _validate(manifest)
    return manifest


def _validate(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ManifestError("manifest root must be an object")
    processes = manifest.get("processes")
    if not isinstance(processes, dict) or not processes:
        raise ManifestError("manifest must define a non-empty 'processes' object")

    for process_id, process in processes.items():
        if not isinstance(process, dict):
            raise ManifestError(f"process '{process_id}' must be an object")
        steps = process.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ManifestError(f"process '{process_id}' must define non-empty 'steps'")
        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                raise ManifestError(f"process '{process_id}' has a non-object step")
            step_id = step.get("id")
            file = step.get("file")
            if not isinstance(step_id, str) or not step_id:
                raise ManifestError(f"process '{process_id}' has a step without an id")
            if step_id in seen:
                raise ManifestError(
                    f"process '{process_id}' has duplicate step id '{step_id}'"
                )
            seen.add(step_id)
            if not isinstance(file, str) or not file:
                raise ManifestError(f"step '{process_id}.{step_id}' has no file")
        # prerequisites are step ids of the same process (tracked in the DB)
        for step in steps:
            for req in step.get("requires", []):
                if req not in seen:
                    raise ManifestError(
                        f"step '{process_id}.{step['id']}' requires unknown step '{req}'"
                    )


def process_ids() -> list[str]:
    """All process ids in manifest order."""
    return list(load_manifest()["processes"])


def get_process(process_id: str) -> dict[str, Any]:
    """Return one process definition or raise ManifestError."""
    manifest = load_manifest()
    process = manifest["processes"].get(process_id)
    if process is None:
        known = ", ".join(sorted(manifest["processes"]))
        raise ManifestError(f"unknown process '{process_id}' (known: {known})")
    return process


def get_step(process_id: str, step_id: str) -> dict[str, Any]:
    """Return one step definition or raise ManifestError."""
    process = get_process(process_id)
    for step in process["steps"]:
        if step["id"] == step_id:
            return step
    known = ", ".join(step["id"] for step in process["steps"])
    raise ManifestError(
        f"unknown step '{step_id}' in process '{process_id}' (known: {known})"
    )


def slug_steps(process_id: str) -> list[str]:
    """Step ids of a process that operate on a slug (e.g. bugfix/assess flows)."""
    return list(load_manifest().get("slug_steps", {}).get(process_id, []))


def next_step_id(process_id: str, completed: set[str]) -> str | None:
    """First incomplete non-optional step, else first incomplete optional one."""
    steps = get_process(process_id)["steps"]
    for optional in (False, True):
        for step in steps:
            if step["id"] not in completed and bool(step.get("optional")) is optional:
                return step["id"]
    return None