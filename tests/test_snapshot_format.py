from __future__ import annotations

import pytest

from kanban_store.snapshot_format import (
    CANONICAL_METADATA,
    SnapshotFormatError,
    normalize_snapshot,
    validate_snapshot,
)


def _snapshot(*, task_overrides: dict | None = None, project_overrides: dict | None = None):
    project = {
        "id": "default",
        "name": "Default",
        "color": "#F10D30",
        "icon": "D",
        "sort_order": 0,
        "archived": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "path": None,
        "task_counts": {"backlog": 1},
        "total_tasks": 1,
    }
    project.update(project_overrides or {})
    task = {
        "id": "T-001",
        "title": "A task",
        "status": "backlog",
        "priority": "normal",
        "size": "M",
        "assignee": None,
        "description": "",
        "acceptance": "",
        "external_blocker": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "moved_at": "2026-01-01T00:00:00+00:00",
        "column_order": 0,
        "project_id": "default",
        "links": [{"type": "url", "value": "https://example.com"}],
        "blockers": [],
        "history": [],
    }
    task.update(task_overrides or {})
    return {
        "exported_at": "2026-01-01T00:00:00+00:00",
        **CANONICAL_METADATA,
        "projects": [project],
        "tasks": [task],
    }


def test_normalize_adds_canonical_metadata_and_preserves_unknown_fields():
    raw = _snapshot()
    raw.pop("snapshot_version")
    raw.pop("database_schema_version")
    raw["future_metadata"] = {"kept": True}

    normalized = normalize_snapshot(raw)

    assert normalized["schema_version"] == 2
    assert normalized["snapshot_version"] == 1
    assert normalized["database_schema_version"] == 2
    assert normalized["future_metadata"] == {"kept": True}


def test_legacy_optional_task_fields_get_safe_defaults():
    raw = _snapshot(
        task_overrides={
            "project_id": None,
            "links": None,
            "blockers": None,
            "history": None,
        }
    )
    # These keys represent omission in a legacy export, rather than invalid nulls.
    for key in ("project_id", "links", "blockers", "history"):
        raw["tasks"][0].pop(key)

    normalized = normalize_snapshot(raw)
    task = normalized["tasks"][0]

    assert task["project_id"] == "default"
    assert task["links"] == []
    assert task["blockers"] == []
    assert task["history"] == []


def test_validate_accepts_mapping_subclasses_without_mutating_input():
    raw = _snapshot()
    before = raw.copy()

    validate_snapshot(raw)

    assert raw == before


@pytest.mark.parametrize(
    "raw",
    [None, [], "snapshot", {"schema_version": 2, "projects": {}, "tasks": []}],
)
def test_rejects_malformed_roots(raw):
    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(raw)


def test_rejects_unsupported_future_snapshot_version():
    raw = _snapshot()
    raw["snapshot_version"] = 2

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(raw)


def test_rejects_unsupported_future_database_version():
    raw = _snapshot()
    raw["database_schema_version"] = 6

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(raw)


@pytest.mark.parametrize("field", ["id", "title", "status", "column_order"])
def test_rejects_malformed_required_task_types(field):
    raw = _snapshot()
    raw["tasks"][0][field] = []

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(raw)


def test_rejects_duplicate_task_and_project_ids():
    duplicate_task = _snapshot()
    duplicate_task["tasks"].append(dict(duplicate_task["tasks"][0]))
    duplicate_project = _snapshot()
    duplicate_project["projects"].append(dict(duplicate_project["projects"][0]))

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(duplicate_task)
    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(duplicate_project)


def test_rejects_invalid_project_and_blocker_references():
    missing_project = _snapshot(task_overrides={"project_id": "missing"})
    missing_blocker = _snapshot(task_overrides={"blockers": ["T-404"]})

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(missing_project)
    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(missing_blocker)


def test_rejects_history_for_a_different_task():
    raw = _snapshot(
        task_overrides={
            "history": [
                {
                    "id": 1,
                    "task_id": "T-404",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "actor": "user",
                    "action": "create",
                    "from_status": None,
                    "to_status": "backlog",
                    "comment": None,
                }
            ]
        }
    )

    with pytest.raises(SnapshotFormatError):
        normalize_snapshot(raw)
