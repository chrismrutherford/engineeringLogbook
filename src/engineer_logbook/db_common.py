"""Shared helpers for the engineer logbook storage layer.

Timestamps are ISO 8601 strings; tags are comma-separated strings.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

VALID_PRIORITIES = ("low", "normal", "high")
VALID_TASK_STATUSES = ("open", "done")
SEARCHABLE_TYPES = ("log", "note", "decision", "snippet", "spec_artifact")

DEFAULT_IDLE_MINUTES = 60

# Owner of rows written by this process (the user_id column; later the OAuth
# 'sub' of the caller). Existing data was backfilled to this value by
# migration 0003.
DEFAULT_USER_ID = "chris"

_CACHED_CHAT_ID: str | None = None


def user_id() -> str:
    """User id stamped on new rows via the logbook.user_id connection setting.

    ENGINEER_LOGBOOK_USER_ID overrides the default. Single-user for now: one
    id per process. Per-request identity (OAuth) will set it per checkout.
    """
    return os.environ.get("ENGINEER_LOGBOOK_USER_ID", "").strip() or DEFAULT_USER_ID


def chat_id() -> str:
    """Identity of the calling chat.

    One MCP server process serves one chat (stdio), so process identity is a
    stable per-chat key. ENGINEER_LOGBOOK_CHAT_ID overrides it, which also
    lets tests group or separate chats on demand. The generated id is cached
    so every call in one process shares the same identity.
    """
    global _CACHED_CHAT_ID
    if _CACHED_CHAT_ID is not None:
        return _CACHED_CHAT_ID
    env = os.environ.get("ENGINEER_LOGBOOK_CHAT_ID")
    if env:
        _CACHED_CHAT_ID = env
    else:
        _CACHED_CHAT_ID = f"pid{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return _CACHED_CHAT_ID


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _idle_cutoff(now: datetime, idle_minutes: float) -> str:
    return (now - timedelta(minutes=idle_minutes)).isoformat(timespec="seconds")


def _tag_str(tags: list[str] | None) -> str:
    if not tags:
        return ""
    return ",".join(t.strip() for t in tags if t and t.strip())


def _has_tags(row_tags: str, wanted: list[str]) -> bool:
    have = {t.strip().lower() for t in (row_tags or "").split(",") if t.strip()}
    return {w.strip().lower() for w in wanted} <= have


def _validate_date(value: str, field: str = "date") -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid {field} {value!r}; expected YYYY-MM-DD.")
    return value


def _norm_time(value: str, upper: bool) -> str:
    """Normalize a date/datetime filter bound.

    Date-only values expand to the start of that day (or, for upper bounds,
    to the start of the *next* day, so the whole day is covered).
    """
    v = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if len(v) == 10:
        if upper:
            v = (datetime.fromisoformat(v) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        else:
            v += "T00:00:00"
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(
            f"Invalid date/time {value!r}; use YYYY-MM-DD or a full ISO timestamp."
        )
    return v


def _fts_query(query: str) -> str:
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        raise ValueError("Empty search query.")
    return " ".join(f'"{t}"' for t in tokens)


def _dump_context(context: dict) -> str:
    return json.dumps(context, ensure_ascii=False, indent=2)


def _tokens(title: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in title).split() if len(t) > 3]