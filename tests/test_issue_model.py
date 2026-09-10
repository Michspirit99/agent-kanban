"""Phase 1 foundation: canonical Issue model with additive fields.

``Task`` remains the compatibility surface; new issue fields (issue_type,
reporter, labels, custom_fields, updated_at) are additive and default safely
for existing data, clients, and snapshots.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from kanban_mcp import server as mcp_server
from kanban_store import Store
from kanban_ui import main


V4_SCHEMA = """
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#F10D30',
    icon        TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    path        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'backlog',
    priority        TEXT NOT NULL DEFAULT 'normal',
    size            TEXT NOT NULL DEFAULT 'M',
    assignee        TEXT,
    description     TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '',
    external_blocker TEXT,
    created_at      TEXT NOT NULL,
    moved_at        TEXT NOT NULL,
    column_order    INTEGER NOT NULL DEFAULT 0,
    project_id      TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE task_links (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type     TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (task_id, type, value)
);
CREATE TABLE task_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    comment      TEXT
);
CREATE TABLE task_blockers (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, blocker_id),
    CHECK (task_id != blocker_id)
);
CREATE TABLE project_sources (
    project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    config        TEXT NOT NULL,
    last_sync_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta(key, value) VALUES ('schema_version', '4');
INSERT INTO meta(key, value) VALUES ('next_id', '2');
INSERT INTO projects(id, name, color, icon, sort_order, archived, created_at)
    VALUES ('default', 'Default', '#F10D30', 'D', 0, 0, '2026-01-01T00:00:00+00:00');
INSERT INTO tasks(
    id, title, status, priority, size, assignee, description, acceptance,
    external_blocker, created_at, moved_at, column_order, project_id
) VALUES (
    'T-001', 'Legacy v4 task', 'approved', 'high', 'M', NULL, 'Keep me',
    'Still valid', NULL, '2026-01-01T00:00:00+00:00',
    '2026-01-02T00:00:00+00:00', 0, 'default'
);
"""


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    value = Store(tmp_path / "tasks.db")
    yield value
    value.close()


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db = Store(tmp_path / "api.db")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


def test_v4_database_gains_issue_fields_preserving_data(tmp_path):
    path = tmp_path / "legacy-v4.db"
    conn = sqlite3.connect(path)
    conn.executescript(V4_SCHEMA)
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "7"

        task = store.get_task("T-001")
        assert task.title == "Legacy v4 task"
        assert task.issue_type == "task"
        assert task.reporter is None
        assert task.labels == []
        assert task.custom_fields == {}
        assert task.updated_at == task.created_at
    finally:
        store.close()


def test_fresh_issues_have_canonical_defaults(store):
    task = store.create_task("Canonical issue")

    assert task.summary == task.title == "Canonical issue"
    assert task.issue_type == "task"
    assert task.reporter is None
    assert task.labels == []
    assert task.custom_fields == {}
    assert task.updated_at == task.created_at


def test_create_with_issue_metadata_persists(store):
    task = store.create_task(
        "Bug report",
        issue_type="bug",
        reporter="user",
        labels=["api", "sqlite"],
    )

    fetched = store.get_task(task.id)
    assert fetched.issue_type == "bug"
    assert fetched.reporter == "user"
    assert fetched.labels == ["api", "sqlite"]
    assert fetched.summary == "Bug report"


def test_invalid_issue_type_rejected_without_insert(store):
    with pytest.raises(ValueError, match="issue_type"):
        store.create_task("Bad", issue_type="")

    assert store.list_tasks() == []
    assert store._conn.execute(
        "SELECT value FROM meta WHERE key='next_id'"
    ).fetchone()["value"] == "1"


def test_update_replaces_labels_and_touches_updated_at_but_move_does_not(store):
    task = store.create_task("Track me", labels=["first"])

    updated = store.update_fields(task.id, actor="user", labels=["second", "third"])
    assert updated.labels == ["second", "third"]
    assert updated.updated_at >= task.created_at
    updated_at_after_edit = updated.updated_at

    moved = store.move_task(task.id, "testing")
    assert moved.updated_at == updated_at_after_edit

    renamed = store.update_fields(task.id, actor="user", title="Track me 2")
    assert renamed.updated_at >= updated_at_after_edit


def test_rest_create_and_patch_carry_issue_fields(api_client):
    client, db = api_client

    created = client.post(
        "/api/tasks",
        json={
            "title": "REST issue",
            "issue_type": "story",
            "reporter": "user",
            "labels": ["backend", "urgent"],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["issue_type"] == "story"
    assert body["reporter"] == "user"
    assert body["labels"] == ["backend", "urgent"]
    assert body["summary"] == "REST issue"

    patched = client.patch(
        f"/api/tasks/{body['id']}",
        json={"labels": ["backend"]},
    )
    assert patched.status_code == 200
    assert patched.json()["labels"] == ["backend"]
    assert db.get_task(body["id"]).labels == ["backend"]


def test_mcp_create_and_update_carry_issue_fields(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", store)

    created = mcp_server.kanban_create(
        "MCP issue", issue_type="epic", labels=["planning"]
    )
    assert created["ok"] is True
    assert created["data"]["issue_type"] == "epic"
    assert created["data"]["labels"] == ["planning"]

    updated = mcp_server.kanban_update(created["data"]["id"], labels=["planning", "q3"])
    assert updated["ok"] is True
    assert updated["data"]["labels"] == ["planning", "q3"]


def test_snapshot_round_trip_preserves_issue_fields(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    task = store.create_task(
        "Rich issue", issue_type="bug", reporter="user", labels=["alpha"]
    )
    payload = store.snapshot()

    target = Store(tmp_path / "target.db")
    try:
        target.import_snapshot(payload)
        imported = target.get_task(task.id)
        assert imported.issue_type == "bug"
        assert imported.reporter == "user"
        assert imported.labels == ["alpha"]
        assert imported.updated_at == task.updated_at
    finally:
        target.close()


def test_legacy_snapshot_import_gets_safe_defaults(store, tmp_path):
    store.create_task("Legacy import me")
    payload = store.snapshot()
    for key in (
        "issue_type", "reporter", "labels", "custom_fields", "updated_at", "summary",
    ):
        payload["tasks"][0].pop(key, None)

    target = Store(tmp_path / "target.db")
    try:
        report = target.import_snapshot(payload)
        assert report["tasks"] == 1
        imported = target.get_task("T-001")
        assert imported.issue_type == "task"
        assert imported.labels == []
        assert imported.updated_at == imported.created_at
    finally:
        target.close()
