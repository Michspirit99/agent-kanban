"""Event payload builders for the durable outbox (Phase 3.1).

Pure functions: they build the exact payload shapes the webhook endpoints
used to emit inline (``main.py``), so webhook consumers see no change. The
Store records these payloads into the ``issue_events`` table in the same
transaction as the mutation; a background dispatcher delivers them.

Supported event types:

- ``task_created``   {task, project}
- ``task_moved``     {task, project, from_status, to_status, comment}
- ``task_updated``   {task, project, changed_fields}
- ``task_commented`` {task, project, comment}
"""
from __future__ import annotations

from typing import Any

EVENT_TYPES = frozenset(
    {"task_created", "task_moved", "task_updated", "task_commented"}
)

# Mutations performed by this actor are not recorded as events: the rule
# engine applies its actions with actor="automation" and must not recurse
# into itself through the outbox.
AUTOMATION_ACTOR = "automation"


def task_created_payload(
    task: Any, project: Any | None = None
) -> dict[str, Any]:
    return {
        "task": task.to_public(),
        "project": project.to_public() if project else None,
    }


def task_moved_payload(
    task: Any,
    project: Any | None,
    from_status: str,
    to_status: str,
    comment: str | None,
) -> dict[str, Any]:
    return {
        "task": task.to_public(),
        "project": project.to_public() if project else None,
        "from_status": from_status,
        "to_status": to_status,
        "comment": comment,
    }


def task_updated_payload(
    task: Any, project: Any | None, changed_fields: list[str]
) -> dict[str, Any]:
    return {
        "task": task.to_public(),
        "project": project.to_public() if project else None,
        "changed_fields": list(changed_fields),
    }


def task_commented_payload(
    task: Any, project: Any | None, comment: str
) -> dict[str, Any]:
    return {
        "task": task.to_public(),
        "project": project.to_public() if project else None,
        "comment": comment,
    }


__all__ = [
    "AUTOMATION_ACTOR",
    "EVENT_TYPES",
    "task_commented_payload",
    "task_created_payload",
    "task_moved_payload",
    "task_updated_payload",
]
