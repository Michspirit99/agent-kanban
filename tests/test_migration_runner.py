"""Migration runner tests: transactional, idempotent schema upgrades with
integrity checks. Complements the Store-level migration tests."""
from __future__ import annotations

import logging
import sqlite3

import pytest

from kanban_store import Store
from kanban_store import migrations as mig


V1_SCHEMA = """
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


def _make_v1_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.commit()
    conn.close()


def test_apply_migrations_returns_latest_version(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")

    version = mig.apply_migrations(conn)

    assert version == mig.LATEST_VERSION == 5
    conn.close()


def test_store_sets_busy_timeout(tmp_path):
    store = Store(tmp_path / "tasks.db")
    try:
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == mig.BUSY_TIMEOUT_MS
    finally:
        store.close()


def test_reopen_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")

    first = Store(tmp_path / "tasks.db")
    first.create_task("One")
    first.close()

    for _ in range(2):
        reopened = Store(tmp_path / "tasks.db")
        try:
            version = reopened._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            assert version == "5"
            assert len(reopened.list_tasks()) == 1
            assert reopened._conn.execute(
                "SELECT value FROM meta WHERE key='next_id'"
            ).fetchone()["value"] == "2"
        finally:
            reopened.close()


def test_failing_migration_rolls_back_version_and_data(tmp_path):
    path = tmp_path / "legacy.db"
    _make_v1_db(path)
    conn = sqlite3.connect(path)

    def boom(_conn):
        raise RuntimeError("boom-injected")

    # v2/v3/v4 commit (version advances to 4); the failing v5 step must roll
    # back without advancing the version or corrupting committed data.
    failing = mig.MIGRATIONS[:-1] + [(5, "boom", boom, False)]

    with pytest.raises(RuntimeError, match="boom-injected"):
        mig.apply_migrations(conn, migrations=failing)

    assert conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    conn.close()

    store = Store(path)
    try:
        assert store.get_task("T-001") is not None
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "5"
    finally:
        store.close()


def test_foreign_key_violations_warn_but_store_opens(tmp_path, caplog):
    path = tmp_path / "violations.db"
    first = Store(path)
    first.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute(
        "INSERT INTO task_history (task_id, ts, actor, action) "
        "VALUES ('T-404', '2026-01-01T00:00:00+00:00', 'legacy', 'comment')"
    )
    raw.commit()
    raw.close()

    with caplog.at_level(logging.WARNING, logger="kanban.store.migrations"):
        reopened = Store(path)
    try:
        assert "foreign_key_check" in caplog.text
        task = reopened.create_task("Still works")
        assert reopened.get_task(task.id) is not None
    finally:
        reopened.close()
