from __future__ import annotations

from kanban_store import Store


def test_store_uses_kanban_db_environment_path(monkeypatch, tmp_path):
    db_path = tmp_path / "configured" / "kanban.db"
    monkeypatch.setenv("KANBAN_DB", str(db_path))

    store = Store()
    try:
        assert store.db_path == db_path
        assert db_path.exists()
    finally:
        store.close()


def test_store_reopens_data_from_kanban_db_environment_path(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("KANBAN_DB", str(db_path))

    first = Store()
    try:
        assert first.db_path == db_path
        project_id = "configured"
        first.create_project(project_id, "Configured")
        first.create_task("Persisted task", project_id=project_id)
    finally:
        first.close()

    second = Store()
    try:
        assert second.db_path == db_path
        tasks = second.list_tasks(project_id=project_id)
        assert [task.title for task in tasks] == ["Persisted task"]
    finally:
        second.close()
