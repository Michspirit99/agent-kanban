from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3

import pytest

from kanban_store import Store
import kanban_store.store as store_module


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    value = Store(tmp_path / "tasks.db")
    yield value
    value.close()


def _empty_store(tmp_path):
    # Keep the production bootstrap row to verify normal imports into a fresh
    # Store, rather than only testing against an artificially empty database.
    return Store(tmp_path / "target.db")


def _rich_store(store):
    archived = store.create_project(
        "archived", "Archived", color="#123456", icon="A", sort_order=2
    )
    store.archive_project(archived.id)
    task = store.create_task(
        "Rich task",
        status="approved",
        priority="high",
        size="L",
        description="Details",
        acceptance="It is accepted",
        assignee="agent:test",
        external_blocker="Waiting on review",
        project_id=archived.id,
        links=[{"type": "url", "value": "https://example.test"}],
    )
    blocker = store.create_task("Blocker", project_id=archived.id)
    store.set_blockers(task.id, [blocker.id])
    store.move_task(task.id, "in_progress", actor="agent:test", comment="started")
    store.add_comment(task.id, "A complete comment", actor="user")
    store.update_fields(task.id, actor="user", title="Rich task updated")
    return archived, task, blocker


def test_snapshot_exports_canonical_metadata_and_complete_content(store):
    archived, task, blocker = _rich_store(store)
    store._conn.execute(
        "UPDATE meta SET value='3' WHERE key='schema_version'"
    )

    snapshot = store.snapshot()

    assert snapshot["schema_version"] == 2
    assert snapshot["snapshot_version"] == 1
    assert snapshot["database_schema_version"] == 3
    assert {project["id"] for project in snapshot["projects"]} == {
        "default",
        archived.id,
    }
    archived_export = next(p for p in snapshot["projects"] if p["id"] == archived.id)
    assert archived_export["archived"] is True

    exported_task = next(t for t in snapshot["tasks"] if t["id"] == task.id)
    assert exported_task["title"] == "Rich task updated"
    assert exported_task["priority"] == "high"
    assert exported_task["links"] == [{"type": "url", "value": "https://example.test"}]
    assert exported_task["blockers"] == [blocker.id]
    assert exported_task["history"] == [
        asdict(history) for history in store.get_task(task.id).history
    ]


def test_save_snapshot_uses_atomic_writer_and_keeps_path_contract(store, tmp_path, monkeypatch):
    calls = []

    def write(destination, payload):
        calls.append((destination, payload))
        destination.write_text(json.dumps(payload), encoding="utf-8")
        return destination

    monkeypatch.setattr(store_module, "write_json_atomic", write)

    result = store.save_snapshot(tmp_path / "snapshots")

    assert result.parent == tmp_path / "snapshots"
    assert result.name.endswith(".json")
    assert calls[0][0] == result
    assert json.loads(result.read_text(encoding="utf-8")) == calls[0][1]


def test_snapshot_round_trip_into_fresh_store_preserves_rich_data(store, tmp_path):
    archived, task, blocker = _rich_store(store)
    payload = store.snapshot()
    target = _empty_store(tmp_path)
    try:
        target.import_snapshot(payload)

        target_projects = {project["id"] for project in target.snapshot()["projects"]}
        payload_projects = {project["id"] for project in payload["projects"]}
        assert target_projects == payload_projects
        imported = target.get_task(task.id)
        assert imported is not None
        assert imported.to_public() == next(t for t in payload["tasks"] if t["id"] == task.id)
        assert target.get_project(archived.id).archived is True
        assert target.get_task(blocker.id) is not None
    finally:
        target.close()


def test_repeated_identical_import_is_idempotent(store, tmp_path):
    _rich_store(store)
    payload = store.snapshot()
    target = _empty_store(tmp_path)
    try:
        first = target.import_snapshot(payload)
        second = target.import_snapshot(payload)

        assert first["projects"] == 1
        assert first["tasks"] == 2
        assert first["history"] == 5
        assert second == {"projects": 0, "tasks": 0, "links": 0, "blockers": 0, "history": 0}
        assert target.snapshot()["tasks"] == payload["tasks"]
    finally:
        target.close()


def test_conflict_rolls_back_the_entire_import(store, tmp_path):
    archived, task, _ = _rich_store(store)
    payload = store.snapshot()
    target = _empty_store(tmp_path)
    try:
        target.create_project(
            archived.id,
            "Different name",
            color=archived.color,
            icon=archived.icon,
            sort_order=archived.sort_order,
        )

        with pytest.raises(ValueError):
            target.import_snapshot(payload)

        assert target.get_task(task.id) is None
        assert target.get_project(archived.id).name == "Different name"
    finally:
        target.close()


def test_invalid_status_is_rejected_before_import(store, tmp_path):
    store.create_task("Task")
    payload = store.snapshot()
    payload["tasks"][0]["status"] = "unknown"
    target = _empty_store(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown task status"):
            target.import_snapshot(payload)
        assert target.get_task("T-001") is None
    finally:
        target.close()


def test_insert_failure_rolls_back_all_imported_rows(store, tmp_path):
    _rich_store(store)
    payload = store.snapshot()
    target = _empty_store(tmp_path)
    try:
        target._conn.execute(
            """
            CREATE TRIGGER fail_snapshot_task_insert
            AFTER INSERT ON tasks
            BEGIN
                SELECT RAISE(ABORT, 'injected snapshot failure');
            END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected snapshot failure"):
            target.import_snapshot(payload)
        assert target.get_project("archived") is None
        assert target.get_task("T-001") is None
        assert target._conn.execute(
            "SELECT value FROM meta WHERE key='next_id'"
        ).fetchone()["value"] == "1"
    finally:
        target.close()


def test_import_advances_next_id_without_changing_schema_version(store, tmp_path):
    payload = {
        "exported_at": "2026-01-01T00:00:00+00:00",
        "schema_version": 2,
        "snapshot_version": 1,
        "database_schema_version": 4,
        "projects": [
            {
                "id": "imported",
                "name": "Imported",
                "color": "#123456",
                "icon": "I",
                "sort_order": 0,
                "archived": False,
                "created_at": "2026-01-01T00:00:00+00:00",
                "path": None,
            }
        ],
        "tasks": [
            {
                "id": "T-042",
                "title": "Imported task",
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
                "project_id": "imported",
                "links": [],
                "blockers": [],
                "history": [],
            }
        ],
    }
    target = _empty_store(tmp_path)
    try:
        before = target._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        target.import_snapshot(payload)
        after = target._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]

        assert before == after == "9"
        assert target._conn.execute(
            "SELECT value FROM meta WHERE key='next_id'"
        ).fetchone()["value"] == "43"
        assert target.create_task("Next", project_id="imported").id == "T-043"
    finally:
        target.close()
