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
DEFAULT_ENFORCE_OWNERS = False
DEFAULT_WORKFLOW_SETTINGS: dict[str, Any] = {
    "claim_from": DEFAULT_CLAIM_FROM,
    "claim_to": DEFAULT_CLAIM_TO,
    "active_statuses": list(DEFAULT_ACTIVE_STATUSES),
    "enforce_owners": DEFAULT_ENFORCE_OWNERS,
}

# Actor prefixes classified as agents (rule engine, Claude Code, named agents).
# Everything else is treated as a human user. Documented heuristic; name your
# human actor anything outside these prefixes.
_AGENT_ACTOR_PREFIXES = ("automation", "claude", "agent")


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
    used by ``pull_task``), ``active_statuses`` (statuses counted as
    active work, e.g. by MCP ``kanban_my_active``), and ``enforce_owners``
    (opt-in transition enforcement against status owners).
    """
    settings = dict(workflow.settings or {})
    settings.setdefault("claim_from", DEFAULT_CLAIM_FROM)
    settings.setdefault("claim_to", DEFAULT_CLAIM_TO)
    active = settings.get("active_statuses")
    if not isinstance(active, list) or not all(
        isinstance(status, str) for status in active
    ):
        settings["active_statuses"] = list(DEFAULT_ACTIVE_STATUSES)
    enforce = settings.get("enforce_owners")
    if not isinstance(enforce, bool):
        settings["enforce_owners"] = DEFAULT_ENFORCE_OWNERS
    return settings


def actor_kind(actor: str) -> str:
    """Classify an actor as ``"agent"`` or ``"user"``.

    Agents: the rule engine (``automation``), Claude Code (``claude...``)
    and named agents (``agent``, ``agent:<name>``). Everything else is a
    human user.
    """
    a = (actor or "").strip().lower()
    if a.startswith(_AGENT_ACTOR_PREFIXES):
        return "agent"
    return "user"


def actor_may_enter(workflow: Workflow, to_status: str, actor: str) -> bool:
    """Whether *actor* may move a task INTO ``to_status`` under *workflow*.

    Enforcement is opt-in via the workflow's ``enforce_owners`` setting;
    when off (the default) everything is allowed. When on, the target
    status's owner must accept the actor's kind (``user``/``agent``;
    ``any`` accepts both). Unknown statuses are denied here — the caller's
    own validation reports them with a precise message.
    """
    settings = workflow_settings(workflow)
    if not settings["enforce_owners"]:
        return True
    status = next((s for s in workflow.statuses if s.key == to_status), None)
    if status is None:
        return False
    owner = status.owner
    if owner == "any":
        return True
    return owner == actor_kind(actor)


def transition_allowed(workflow: Workflow, from_status: str, to_status: str) -> bool:
    """Whether the workflow's optional transition graph allows from → to.

    Opt-in via the workflow's ``transitions`` setting: a map of
    ``from_status`` → list of allowed target statuses, e.g.
    ``{"backlog": ["approved", "blocked"]}``. Statuses absent from the
    map have unrestricted outgoing transitions; the whole setting absent
    (or malformed) means no structural restriction at all. Independent of
    ``enforce_owners`` — graphs constrain structure, owners constrain
    actors.
    """
    graph = workflow_settings(workflow).get("transitions")
    if not isinstance(graph, dict):
        return True
    allowed = graph.get(from_status)
    if not isinstance(allowed, list) or not all(isinstance(s, str) for s in allowed):
        return True
    return to_status in allowed


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
    "DEFAULT_ENFORCE_OWNERS",
    "DEFAULT_STATUS_DEFINITIONS",
    "DEFAULT_WORKFLOW_SETTINGS",
    "STATUS_KEY_RE",
    "VALID_OWNERS",
    "WORKFLOW_ID_RE",
    "Workflow",
    "WorkflowError",
    "WorkflowStatus",
    "actor_kind",
    "actor_may_enter",
    "default_workflow",
    "transition_allowed",
    "validate_workflow_statuses",
    "workflow_settings",
]
