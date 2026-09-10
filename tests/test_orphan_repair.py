"""Orphaned-task repair (Phase 4.2a, migration v9).

Legacy databases can contain tasks whose project_id has no matching project
(the column predates FK-style validation). Such tasks render on no board and
are invisible. Migration v9 reparents them to the default project, appending
each to the end of its column, preserving all child data (history, links,
blockers). The Store rejects new orphans; this repairs legacy rows.

The full FK constraint via a tasks-table rebuild was evaluated and rejected:
child tables declare ON DELETE CASCADE, so rebuilding with foreign_keys ON
would wipe links/history, and the pragma cannot be toggled inside the
runner's transaction. Store-level validation remains the enforcement.
"""
from __future__ import annotations

import sqlite3

import pytest

from kanban_store import Store
from kanban_store.migrations import MIGRATIONS, apply_migrations


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """A v8 database containing an orphaned task plus its child rows."""
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db_path = tmp_path / "orphans.db"
    conn = sqlite3.connect(str(db_path))
    apply_migrations(conn, migrations=MIGRATIONS[:-1])  # stop at v8
    # a healthy default-project task in backlog, column_order 0
    conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-001', 'Healthy task', 'backlog', 'normal', 'M', NULL,
                   '', '', NULL, '2026-01-01', '2026-01-01', 0, 'default',
                   'task', NULL, '[]', '{}', '2026-01-01')"""
    )
    # an orphan: project 'ghost' does not exist
    conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-002', 'Lost task', 'backlog', 'normal', 'M', NULL,
                   'hidden work', '', NULL, '2026-01-02', '2026-01-02', 3,
                   'ghost', 'task', NULL, '[]', '{}', '2026-01-02')"""
    )
    conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-003', 'Another healthy', 'in_progress', 'normal', 'M',
                   NULL, '', '', NULL, '2026-01-01', '2026-01-01', 0,
                   'default', 'task', NULL, '[]', '{}', '2026-01-01')"""
    )
    # child rows for the orphan: history, link, blocker
    conn.execute(
        """INSERT INTO task_history (task_id, ts, actor, action, from_status,
                                     to_status, comment)
           VALUES ('T-002', '2026-01-02', 'user', 'create', NULL, 'backlog', NULL)"""
    )
    conn.execute(
        "INSERT INTO task_links (task_id, type, value) VALUES ('T-002', 'url', 'https://example.com')"
    )
    conn.execute(
        "INSERT INTO task_blockers (task_id, blocker_id) VALUES ('T-002', 'T-001')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_v9_reparents_orphans_to_default_project(legacy_db):
    store = Store(legacy_db)
    try:
        assert store.schema_version() == 9

        task = store.get_task("T-002")
        assert task is not None, "orphaned task vanished"
        assert task.project_id == "default"

        # visible on the board now, appended after the healthy backlog task
        assert task.column_order == 1
        in_default = [t.id for t in store.list_tasks(project_id="default")]
        assert "T-002" in in_default

        # the repair invariant: no task references a missing project anymore
        orphans = store._conn.execute(
            "SELECT t.id FROM tasks t "
            "LEFT JOIN projects p ON p.id = t.project_id "
            "WHERE p.id IS NULL"
        ).fetchall()
        assert orphans == []
    finally:
        store.close()


def test_v9_preserves_child_data(legacy_db):
    store = Store(legacy_db)
    try:
        task = store.get_task("T-002")
        assert task.history, "history lost"
        assert any(h.action == "create" for h in task.history)
        assert ("url", "https://example.com") in [
            (link["type"], link["value"]) for link in task.links
        ]
        assert task.blockers == ["T-001"]
    finally:
        store.close()


def test_v9_untouched_tasks_keep_positions(legacy_db):
    store = Store(legacy_db)
    try:
        healthy = store.get_task("T-001")
        assert healthy.column_order == 0
        in_progress = store.get_task("T-003")
        assert in_progress.project_id == "default"
        assert in_progress.column_order == 0
    finally:
        store.close()


def test_v9_is_idempotent_on_reopen(legacy_db):
    store = Store(legacy_db)
    first = store.get_task("T-002")
    store.close()

    store = Store(legacy_db)
    try:
        assert store.schema_version() == 9
        again = store.get_task("T-002")
        assert again.project_id == first.project_id
        assert again.column_order == first.column_order
    finally:
        store.close()


def test_v9_creates_default_project_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db_path = tmp_path / "no-default.db"
    conn = sqlite3.connect(str(db_path))
    apply_migrations(conn, migrations=MIGRATIONS[:-1])
    # keep one unrelated project so the projects table is non-empty
    conn.execute(
        "INSERT INTO projects (id, name, color, icon, sort_order, archived, created_at) "
        "VALUES ('other', 'Other', '#000000', 'O', 0, 0, '2026-01-01')"
    )
    conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-001', 'Orphan', 'backlog', 'normal', 'M', NULL, '', '',
                   NULL, '2026-01-01', '2026-01-01', 0, 'ghost', 'task', NULL,
                   '[]', '{}', '2026-01-01')"""
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        assert store.get_project("default") is not None
        assert store.get_task("T-001").project_id == "default"
    finally:
        store.close()


def test_v9_leaves_foreign_key_check_clean(legacy_db):
    store = Store(legacy_db)
    try:
        violations = store._conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == []
    finally:
        store.close()
