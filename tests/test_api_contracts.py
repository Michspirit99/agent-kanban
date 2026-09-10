"""Characterization tests: freeze current REST behavior before foundation
refactors. These tests document the existing contract; changes to them mean
a deliberate, reviewed behavior change."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store import Store
from kanban_ui import main


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


STATUS_ORDER = [
    "backlog", "approved", "analyst", "in_progress", "testing",
    "uat", "done", "blocked", "cancelled",
]

PROJECT_KEYS = {
    "id", "name", "color", "icon", "sort_order", "archived",
    "created_at", "path", "task_counts", "total_tasks",
}


def test_board_contract_shape(api_client):
    client, db = api_client
    db.create_task("Board task")

    response = client.get("/api/board", params={"project": "default"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"columns", "tasks", "project"}
    assert [column["id"] for column in body["columns"]] == STATUS_ORDER
    assert set(body["columns"][0]) == {"id", "title", "owner"}
    assert set(body["tasks"]) == set(STATUS_ORDER)
    card = body["tasks"]["backlog"][0]
    assert set(card) == {
        "id", "title", "priority", "size", "assignee",
        "external_blocker", "blockers", "moved_at", "project_id",
    }
    assert set(body["project"]) == PROJECT_KEYS


def test_project_endpoints_contract(api_client):
    client, _ = api_client

    created = client.post("/api/projects", json={"id": "alpha", "name": "Alpha"})
    assert created.status_code == 201
    assert set(created.json()) == PROJECT_KEYS
    assert created.json()["archived"] is False

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert {p["id"] for p in listed.json()["projects"]} >= {"default", "alpha"}

    renamed = client.patch("/api/projects/alpha", json={"name": "Alpha 2"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Alpha 2"

    archived = client.post("/api/projects/alpha/archive", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True


def test_task_lifecycle_contract(api_client):
    client, db = api_client

    created = client.post("/api/tasks", json={"title": "Issue A"})
    assert created.status_code == 201
    task = created.json()
    assert task["id"] == "T-001"
    assert task["status"] == "backlog"
    assert task["priority"] == "normal"
    assert task["size"] == "M"
    assert task["project_id"] == "default"
    assert task["links"] == []
    assert task["blockers"] == []
    assert [h["action"] for h in task["history"]] == ["create"]

    fetched = client.get("/api/tasks/T-001")
    assert fetched.status_code == 200
    assert set(fetched.json()) == set(task)

    patched = client.patch("/api/tasks/T-001", json={"title": "Issue A renamed"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "Issue A renamed"

    moved = client.post(
        "/api/tasks/T-001/move", json={"to_status": "testing", "comment": "ready"}
    )
    assert moved.status_code == 200
    assert moved.json()["status"] == "testing"

    history = client.get("/api/tasks/T-001").json()["history"]
    assert [h["action"] for h in history] == ["create", "update", "move"]
    move_entry = history[-1]
    assert move_entry["from_status"] == "backlog"
    assert move_entry["to_status"] == "testing"
    assert set(move_entry) == {
        "id", "task_id", "ts", "actor", "action",
        "from_status", "to_status", "comment",
    }

    commented = client.post("/api/tasks/T-001/comment", json={"text": "note"})
    assert commented.status_code == 201
    assert commented.json() == {"ok": True}

    linked = client.post(
        "/api/tasks/T-001/links", json={"type": "url", "value": "https://example.com"}
    )
    assert linked.status_code == 201
    assert linked.json() == {"ok": True}

    blockers = client.post("/api/tasks/T-001/blockers", json={"blocker_ids": []})
    assert blockers.status_code == 200
    assert blockers.json() == {"ok": True, "blockers": []}


def test_error_contract(api_client):
    client, _ = api_client

    missing = client.get("/api/tasks/T-999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "task T-999 not found"}

    bad_status = client.post("/api/tasks/T-001/move", json={"to_status": "warp"})
    assert bad_status.status_code == 400
    assert "unknown status" in bad_status.json()["detail"]

    bad_project = client.post(
        "/api/tasks", json={"title": "X", "project_id": "nope"}
    )
    assert bad_project.status_code == 400
    assert bad_project.json() == {"detail": "unknown project: nope"}


def test_snapshot_endpoints_contract(api_client, tmp_path, monkeypatch):
    client, db = api_client
    db.create_task("Snapshot task")
    destination = tmp_path / "snapshots"

    def save_snapshot():
        destination.mkdir(parents=True, exist_ok=True)
        fp = destination / "2026-01-01.json"
        fp.write_text("{}", encoding="utf-8")
        return fp

    monkeypatch.setattr(db, "save_snapshot", save_snapshot)

    saved = client.post("/api/snapshot")
    assert saved.status_code == 200
    assert saved.json() == {"ok": True, "path": str(destination / "2026-01-01.json")}

    payload = db.snapshot()
    imported = client.post("/api/snapshot/import", json={"snapshot": payload})
    assert imported.status_code == 200
    assert set(imported.json()) == {"ok", "imported"}
    assert set(imported.json()["imported"]) == {
        "projects", "tasks", "links", "blockers", "history",
    }
    assert imported.json()["imported"] == {
        "projects": 0, "tasks": 0, "links": 0, "blockers": 0, "history": 0,
    }
