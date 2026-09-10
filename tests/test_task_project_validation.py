from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store import Store
from kanban_ui import main
from kanban_mcp import server as mcp_server


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    value = Store(tmp_path / "tasks.db")
    yield value
    value.close()


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    db = Store(tmp_path / "api.db")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


def test_store_rejects_task_for_unknown_project(store):
    with pytest.raises(ValueError, match="project .* not found"):
        store.create_task("Orphan", project_id="missing")

    assert store.list_tasks(project_id="missing") == []
    assert store._conn.execute(
        "SELECT value FROM meta WHERE key='next_id'"
    ).fetchone()["value"] == "1"


def test_mcp_rejects_task_for_unknown_project(monkeypatch, store):
    monkeypatch.setattr(mcp_server, "_store", store)

    result = mcp_server.kanban_create("Orphan", project_id="missing")

    assert result["ok"] is False
    assert "project 'missing' not found" in result["error"]
    assert store.list_tasks(project_id="missing") == []


def test_store_creates_task_for_existing_project(store):
    task = store.create_task("Valid", project_id="default")

    assert task.project_id == "default"
    assert store.get_task(task.id).title == "Valid"


def test_rest_rejects_task_for_unknown_project(api_client):
    client, db = api_client

    response = client.post(
        "/api/tasks",
        json={"title": "Orphan", "project_id": "missing"},
    )

    assert response.status_code == 400
    assert db.list_tasks(project_id="missing") == []
