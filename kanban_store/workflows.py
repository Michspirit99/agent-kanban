"""Central workflow/status registry — pure catalog definitions.

This module has no persistence dependencies. It defines the canonical
workflow model, the built-in default workflow (which reproduces the
original nine-column board exactly), and validation helpers used by the
Store and the API layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

WORKFLOW_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
STATUS_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
VALID_OWNERS = {"user", "agent", "any"}

# Fallback claim/active-status semantics for workflows without explicit
# settings (mirrors the original agent workflow).
DEFAULT_CLAIM_FROM = "approved"
DEFAULT_CLAIM_TO = "analyst"
DEFAULT_ACTIVE_STATUSES = ["analyst", "in_progress", "testing"]
DEFAULT_WORKFLOW_SETTINGS: dict[str, Any] = {
    "claim_from": DEFAULT_CLAIM_FROM,
    "claim_to": DEFAULT_CLAIM_TO,
    "active_statuses": list(DEFAULT_ACTIVE_STATUSES),
}


class WorkflowError(ValueError):
    """Raised when a workflow definition is invalid."""


@dataclass(frozen=True)
class WorkflowStatus:
    key: str
    label: str
    owner: str = "any"
    position: int = 0
    active: bool = True


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    statuses: tuple[WorkflowStatus, ...]
    settings: dict[str, Any] = field(default_factory=dict)

    def status_keys(self) -> list[str]:
        return [status.key for status in self.statuses]

    def has_status(self, key: str) -> bool:
        return any(status.key == key for status in self.statuses)

    def columns(self) -> list[dict[str, str]]:
        """Board column metadata (same shape as the legacy ``status_meta``)."""
        return [
            {"id": status.key, "title": status.label, "owner": status.owner}
            for status in self.statuses
        ]

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "statuses": [
                {
                    "key": status.key,
                    "label": status.label,
                    "owner": status.owner,
                    "position": status.position,
                    "active": status.active,
                }
                for status in self.statuses
            ],
            "settings": workflow_settings(self),
        }


# (key, label, owner) in left-to-right board order — the original model.
DEFAULT_STATUS_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("backlog", "Backlog", "user"),
    ("approved", "Approved", "agent"),
    ("analyst", "Analyst", "agent"),
    ("in_progress", "In progress", "agent"),
    ("testing", "Testing", "agent"),
    ("uat", "UAT", "user"),
    ("done", "Done", "user"),
    ("blocked", "Blocked", "any"),
    ("cancelled", "Cancelled", "user"),
)


def default_workflow() -> Workflow:
    """The built-in workflow every project gets unless overridden."""
    return Workflow(
        id="default",
        name="Default workflow",
        statuses=tuple(
            WorkflowStatus(key=key, label=label, owner=owner, position=position)
            for position, (key, label, owner) in enumerate(DEFAULT_STATUS_DEFINITIONS)
        ),
        settings=dict(DEFAULT_WORKFLOW_SETTINGS),
    )


def workflow_settings(workflow: Workflow) -> dict[str, Any]:
    """Workflow settings with safe defaults filled in.

    Recognized keys: ``claim_from``/``claim_to`` (the agent claim transition
    used by ``pull_task``) and ``active_statuses`` (statuses counted as
    active work, e.g. by MCP ``kanban_my_active``).
    """
    settings = dict(workflow.settings or {})
    settings.setdefault("claim_from", DEFAULT_CLAIM_FROM)
    settings.setdefault("claim_to", DEFAULT_CLAIM_TO)
    active = settings.get("active_statuses")
    if not isinstance(active, list) or not all(
        isinstance(status, str) for status in active
    ):
        settings["active_statuses"] = list(DEFAULT_ACTIVE_STATUSES)
    return settings


def validate_workflow_statuses(raw: list[dict[str, Any]]) -> list[WorkflowStatus]:
    """Validate a raw status list and return ordered WorkflowStatus objects."""
    if not isinstance(raw, list) or not raw:
        raise WorkflowError("statuses must be a non-empty list")
    seen: set[str] = set()
    out: list[WorkflowStatus] = []
    for index, entry in enumerate(raw):
        path = f"statuses[{index}]"
        if not isinstance(entry, dict):
            raise WorkflowError(f"{path} must be an object")
        key = entry.get("key")
        if not isinstance(key, str) or not STATUS_KEY_RE.fullmatch(key):
            raise WorkflowError(
                f"{path}.key must match ^[a-z][a-z0-9_]{{0,31}}$"
            )
        if key in seen:
            raise WorkflowError(f"{path}.key duplicates status key {key!r}")
        seen.add(key)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise WorkflowError(f"{path}.label must be a non-empty string")
        owner = entry.get("owner", "any")
        if owner not in VALID_OWNERS:
            raise WorkflowError(
                f"{path}.owner must be one of {sorted(VALID_OWNERS)}"
            )
        out.append(
            WorkflowStatus(key=key, label=label, owner=owner, position=index)
        )
    return out


__all__ = [
    "DEFAULT_ACTIVE_STATUSES",
    "DEFAULT_CLAIM_FROM",
    "DEFAULT_CLAIM_TO",
    "DEFAULT_STATUS_DEFINITIONS",
    "DEFAULT_WORKFLOW_SETTINGS",
    "STATUS_KEY_RE",
    "VALID_OWNERS",
    "WORKFLOW_ID_RE",
    "Workflow",
    "WorkflowError",
    "WorkflowStatus",
    "default_workflow",
    "validate_workflow_statuses",
    "workflow_settings",
]
