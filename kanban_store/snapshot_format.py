"""Validation and normalization for JSON kanban snapshots.

This module deliberately has no persistence dependencies. It works on already
decoded Python mappings so callers can use it for imports, exports, or tests
without opening a file or database.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, NoReturn

# ``schema_version`` is the field emitted by the original snapshot format.
# Keep it at 2; the two explicit version fields remove its old ambiguity.
LEGACY_SCHEMA_VERSION: Final = 2
SNAPSHOT_VERSION: Final = 1
DATABASE_SCHEMA_VERSION: Final = 4

# Descriptive aliases make the supported versions clear to callers while the
# short names above remain convenient for constructing a snapshot.
CURRENT_SNAPSHOT_VERSION: Final = SNAPSHOT_VERSION
CURRENT_DATABASE_SCHEMA_VERSION: Final = DATABASE_SCHEMA_VERSION

CANONICAL_METADATA: Final[dict[str, int]] = {
    "schema_version": LEGACY_SCHEMA_VERSION,
    "snapshot_version": SNAPSHOT_VERSION,
    "database_schema_version": DATABASE_SCHEMA_VERSION,
}


class SnapshotFormatError(ValueError):
    """Raised when a snapshot is not a supported snapshot mapping."""


def normalize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate *snapshot* and return a new normalized mapping.

    Legacy snapshots identify themselves with ``schema_version=2`` and may
    omit the task fields introduced after that format.  Missing values are
    filled with the same safe defaults used by the store.  Unknown fields are
    retained so a newer producer can round-trip through an older consumer.
    """
    if not isinstance(snapshot, Mapping):
        _error("root must be a mapping")

    root = dict(snapshot)
    schema_version = _required_int(root, "schema_version", "root")
    if schema_version != LEGACY_SCHEMA_VERSION:
        _error(
            "root.schema_version must be 2; the legacy value is retained "
            "for snapshot compatibility"
        )

    snapshot_version = root.get("snapshot_version", SNAPSHOT_VERSION)
    _positive_int(snapshot_version, "root.snapshot_version")
    if snapshot_version > SNAPSHOT_VERSION:
        _error(
            f"root.snapshot_version {snapshot_version} is newer than "
            f"supported version {SNAPSHOT_VERSION}"
        )

    # An old schema_version=2 snapshot predates this explicit field.  Its
    # schema was the v2 database schema, not the current v4 schema.
    database_schema_version = root.get("database_schema_version", schema_version)
    _positive_int(database_schema_version, "root.database_schema_version")
    if database_schema_version > CURRENT_DATABASE_SCHEMA_VERSION:
        _error(
            f"root.database_schema_version {database_schema_version} is newer "
            f"than supported version {CURRENT_DATABASE_SCHEMA_VERSION}"
        )

    _required_string(root, "exported_at", "root")
    projects = _required_list(root, "projects", "root")
    tasks = _required_list(root, "tasks", "root")

    normalized_projects: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    for index, project in enumerate(projects):
        path = f"root.projects[{index}]"
        normalized = _normalize_project(project, path)
        project_id = normalized["id"]
        if project_id in project_ids:
            _error(f"{path}.id duplicates project id {project_id!r}")
        project_ids.add(project_id)
        normalized_projects.append(normalized)

    normalized_tasks: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    for index, task in enumerate(tasks):
        path = f"root.tasks[{index}]"
        normalized = _normalize_task(task, path)
        task_id = normalized["id"]
        if task_id in task_ids:
            _error(f"{path}.id duplicates task id {task_id!r}")
        task_ids.add(task_id)
        normalized_tasks.append(normalized)

    for index, task in enumerate(normalized_tasks):
        path = f"root.tasks[{index}]"
        project_id = task["project_id"]
        if project_id not in project_ids:
            _error(f"{path}.project_id references missing project {project_id!r}")

        for blocker_index, blocker_id in enumerate(task["blockers"]):
            blocker_path = f"{path}.blockers[{blocker_index}]"
            if blocker_id not in task_ids:
                _error(f"{blocker_path} references missing task {blocker_id!r}")
            if blocker_id == task["id"]:
                _error(f"{blocker_path} cannot reference its own task")

        for history_index, history in enumerate(task["history"]):
            history_path = f"{path}.history[{history_index}]"
            history_task_id = history["task_id"]
            if history_task_id not in task_ids:
                _error(
                    f"{history_path}.task_id references missing task "
                    f"{history_task_id!r}"
                )
            if history_task_id != task["id"]:
                _error(
                    f"{history_path}.task_id must match containing task "
                    f"{task['id']!r}"
                )

    root["schema_version"] = schema_version
    root["snapshot_version"] = snapshot_version
    root["database_schema_version"] = database_schema_version
    root["projects"] = normalized_projects
    root["tasks"] = normalized_tasks
    return root


def validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Raise :class:`SnapshotFormatError` unless *snapshot* is supported."""
    normalize_snapshot(snapshot)


def _normalize_project(value: Any, path: str) -> dict[str, Any]:
    project = _mapping(value, path)
    normalized = dict(project)

    project_id = _required_string(project, "id", path, non_empty=True)
    _required_string(project, "name", path)
    _required_string(project, "color", path)
    _required_string(project, "icon", path)
    _required_int(project, "sort_order", path)
    _required_bool(project, "archived", path)
    _required_string(project, "created_at", path)
    normalized["id"] = project_id

    # These are present in current Store snapshots.  They are validated when
    # present, but are not needed to identify or restore a project.
    if "path" in project:
        _string_or_none(project["path"], f"{path}.path")
    if "task_counts" in project:
        counts = _mapping(project["task_counts"], f"{path}.task_counts")
        for status, count in counts.items():
            if not isinstance(status, str):
                _error(f"{path}.task_counts keys must be strings")
            _plain_int(count, f"{path}.task_counts[{status!r}]")
    if "total_tasks" in project:
        _plain_int(project["total_tasks"], f"{path}.total_tasks")

    return normalized


def _normalize_task(value: Any, path: str) -> dict[str, Any]:
    task = _mapping(value, path)
    normalized = dict(task)

    _required_string(task, "id", path, non_empty=True)
    _required_string(task, "title", path)
    _required_string(task, "status", path)
    _required_string(task, "priority", path)
    _required_string(task, "size", path)
    _string_or_none(_required(task, "assignee", path), f"{path}.assignee")
    _required_string(task, "description", path)
    _required_string(task, "acceptance", path)
    _string_or_none(
        _required(task, "external_blocker", path), f"{path}.external_blocker"
    )
    _required_string(task, "created_at", path)
    _required_string(task, "moved_at", path)
    _required_int(task, "column_order", path)

    if "project_id" in task:
        project_id = _required_string(task, "project_id", path, non_empty=True)
    else:
        project_id = "default"
    normalized["project_id"] = project_id

    if "links" in task:
        links = _required_list(task, "links", path)
        normalized["links"] = [
            _normalize_link(link, f"{path}.links[{index}]")
            for index, link in enumerate(links)
        ]
    else:
        normalized["links"] = []

    if "blockers" in task:
        blockers = _required_list(task, "blockers", path)
        normalized_blockers: list[str] = []
        for index, blocker in enumerate(blockers):
            blocker_id = _string(blocker, f"{path}.blockers[{index}]", non_empty=True)
            if blocker_id in normalized_blockers:
                _error(f"{path}.blockers contains duplicate task id {blocker_id!r}")
            normalized_blockers.append(blocker_id)
        normalized["blockers"] = normalized_blockers
    else:
        normalized["blockers"] = []

    if "history" in task:
        history = _required_list(task, "history", path)
        normalized["history"] = [
            _normalize_history(entry, f"{path}.history[{index}]")
            for index, entry in enumerate(history)
        ]
    else:
        normalized["history"] = []

    return normalized


def _normalize_link(value: Any, path: str) -> dict[str, Any]:
    link = _mapping(value, path)
    normalized = dict(link)
    _required_string(link, "type", path)
    _required_string(link, "value", path)
    return normalized


def _normalize_history(value: Any, path: str) -> dict[str, Any]:
    history = _mapping(value, path)
    normalized = dict(history)
    _required_int(history, "id", path)
    _required_string(history, "task_id", path, non_empty=True)
    _required_string(history, "ts", path)
    _required_string(history, "actor", path)
    _required_string(history, "action", path)
    _string_or_none(_required(history, "from_status", path), f"{path}.from_status")
    _string_or_none(_required(history, "to_status", path), f"{path}.to_status")
    _string_or_none(_required(history, "comment", path), f"{path}.comment")
    return normalized


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{path} must be a mapping")
    return dict(value)


def _required(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        _error(f"{path}.{key} is required")
    return mapping[key]


def _required_list(mapping: Mapping[str, Any], key: str, path: str) -> list[Any]:
    value = _required(mapping, key, path)
    if not isinstance(value, list):
        _error(f"{path}.{key} must be a list")
    return value


def _required_string(
    mapping: Mapping[str, Any], key: str, path: str, *, non_empty: bool = False
) -> str:
    return _string(_required(mapping, key, path), f"{path}.{key}", non_empty=non_empty)


def _string(value: Any, path: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str) or (non_empty and not value):
        suffix = " and non-empty" if non_empty else ""
        _error(f"{path} must be a string{suffix}")
    return value


def _string_or_none(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, str):
        _error(f"{path} must be a string or null")


def _plain_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _error(f"{path} must be an integer")
    return value


def _required_int(mapping: Mapping[str, Any], key: str, path: str) -> int:
    return _plain_int(_required(mapping, key, path), f"{path}.{key}")


def _positive_int(value: Any, path: str) -> int:
    number = _plain_int(value, path)
    if number < 1:
        _error(f"{path} must be positive")
    return number


def _required_bool(mapping: Mapping[str, Any], key: str, path: str) -> bool:
    value = _required(mapping, key, path)
    if not isinstance(value, bool):
        _error(f"{path}.{key} must be a boolean")
    return value


def _error(message: str) -> NoReturn:
    raise SnapshotFormatError(message)


__all__ = [
    "CANONICAL_METADATA",
    "CURRENT_DATABASE_SCHEMA_VERSION",
    "CURRENT_SNAPSHOT_VERSION",
    "DATABASE_SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "SNAPSHOT_VERSION",
    "SnapshotFormatError",
    "normalize_snapshot",
    "validate_snapshot",
]
