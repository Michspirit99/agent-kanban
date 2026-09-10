"""Canonical domain models for the issue tracker.

``Issue`` is the canonical model; ``Task`` (kept as an alias in
:mod:`kanban_store.store`) remains the historical public name so existing
task-oriented clients, databases, and snapshots keep working while the
product grows Jira-style issue features additively.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_PROJECT_ID = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")


@dataclass
class TaskHistory:
    id: int
    task_id: str
    ts: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
    comment: str | None


@dataclass
class Issue:
    id: str
    title: str
    status: str
    priority: str
    size: str
    assignee: str | None
    description: str
    acceptance: str
    external_blocker: str | None
    created_at: str
    moved_at: str
    column_order: int
    project_id: str = DEFAULT_PROJECT_ID
    links: list[dict[str, str]] = field(default_factory=list)
    history: list[TaskHistory] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    issue_type: str = "task"
    reporter: str | None = None
    labels: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    @property
    def summary(self) -> str:
        """Canonical Jira-style alias for ``title``."""
        return self.title

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["history"] = [asdict(h) for h in self.history]
        d["summary"] = self.title
        return d


__all__ = ["DEFAULT_PROJECT_ID", "Issue", "TaskHistory"]
