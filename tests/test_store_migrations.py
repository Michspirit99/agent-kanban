from __future__ import annotations

import sqlite3

from kanban_store import Store


def test_fresh_database_is_initialized_idempotently(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "fresh")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Fresh")
    db_path = tmp_path / "fresh.db"

    first = Store(db_path)
    try:
        first.create_task("Original task", project_id="fresh")
        version = first._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "9"
    finally:
        first.close()

    second = Store(db_path)
    try:
        projects = second.list_projects(include_archived=True)
        tasks = second.list_tasks(project_id="fresh")
        assert [project.id for project in projects] == ["fresh"]
        assert [task.id for task in tasks] == ["T-001"]
    finally:
        second.close()


def test_v1_database_migrates_without_losing_tasks(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "legacy")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Legacy")
    db_path = tmp_path / "legacy.db"

    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'backlog',
            priority TEXT NOT NULL DEFAULT 'normal',
            size TEXT NOT NULL DEFAULT 'M',
            assignee TEXT,
            description TEXT NOT NULL DEFAULT '',
            acceptance TEXT NOT NULL DEFAULT '',
            external_blocker TEXT,
            created_at TEXT NOT NULL,
            moved_at TEXT NOT NULL,
            column_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES ('schema_version', '1');
        INSERT INTO meta(key, value) VALUES ('next_id', '2');
        INSERT INTO tasks(
            id, title, status, priority, size, assignee, description,
            acceptance, external_blocker, created_at, moved_at, column_order
        ) VALUES (
            'T-001', 'Legacy task', 'approved', 'high', 'M', NULL, 'Keep me',
            'Still valid', NULL, '2026-01-01T00:00:00+00:00',
            '2026-01-02T00:00:00+00:00', 0
        );
        """
    )
    connection.close()

    store = Store(db_path)
    try:
        task = store.get_task("T-001")
        assert task is not None
        assert task.project_id == "legacy"
        assert task.title == "Legacy task"
        assert task.description == "Keep me"
        assert store.get_project("legacy").name == "Legacy"
        assert store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"] == "9"
    finally:
        store.close()
