"""Phase 2 foundation: central workflow/status registry.

One source of truth for workflow statuses in SQLite; REST, MCP, and Store
validation consume it. The seeded default workflow reproduces the original
nine-column board exactly.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from kanban_mcp import server as mcp_server
from kanban_store import Store, status_meta
from kanban_store.workflows import (
    Workflow,
    WorkflowError,
    default_workflow,
    workflow_settings,
)
from kanban_ui import main
from tests.test_issue_model import V4_SCHEMA


STATUS_ORDER = [
    "backlog", "approved", "analyst", "in_progress", "testing",
    "uat", "done", "blocked", "cancelled",
]


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


def test_default_workflow_matches_legacy_status_model():
    workflow = default_workflow()

    assert workflow.id == "default"
    assert workflow.status_keys() == STATUS_ORDER
    assert workflow.columns() == status_meta()


def test_fresh_store_seeds_default_workflow_and_assigns_projects(store):
    workflows = store.list_workflows()

    assert [w.id for w in workflows] == ["default"]
    assert workflows[0].status_keys() == STATUS_ORDER
    assert store.get_project("default").workflow_id == "default"
    assert store.get_project_workflow("default").status_keys() == STATUS_ORDER


def test_v4_legacy_database_migrates_to_v6_with_workflow(tmp_path):
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
        assert version == "6"
        assert store.get_task("T-001") is not None
        assert store.get_project("default").workflow_id == "default"
        assert store.get_project_workflow("default").status_keys() == STATUS_ORDER
    finally:
        store.close()


def test_create_and_assign_custom_workflow(store):
    store.create_project("qaflow", "QA Flow")

    workflow = store.create_workflow("qaflow-wf", "QA workflow", [
        {"key": "backlog", "label": "Backlog", "owner": "user"},
        {"key": "in_progress", "label": "In progress", "owner": "agent"},
        {"key": "qa_done", "label": "QA Done", "owner": "user"},
    ])
    assert workflow.status_keys() == ["backlog", "in_progress", "qa_done"]

    store.set_project_workflow("qaflow", "qaflow-wf")
    assert store.get_project_workflow("qaflow").id == "qaflow-wf"

    task = store.create_task("Custom status task", status="qa_done", project_id="qaflow")
    assert task.status == "qa_done"
    moved = store.move_task(task.id, "in_progress")
    assert moved.status == "in_progress"

    assert store.get_project_workflow("default").status_keys() == STATUS_ORDER


def test_set_project_workflow_rejects_unmappable_statuses(store):
    store.create_project("team", "Team")
    store.create_task("Needs approved", status="approved", project_id="team")
    store.create_workflow("lean", "Lean", [
        {"key": "backlog", "label": "Backlog", "owner": "user"},
        {"key": "done", "label": "Done", "owner": "user"},
    ])

    with pytest.raises(ValueError, match="statuses not in"):
        store.set_project_workflow("team", "lean")

    assert store.get_project_workflow("team").id == "default"


def test_move_rejected_outside_project_workflow(store):
    store.create_project("team", "Team")
    store.create_workflow("lean", "Lean", [
        {"key": "backlog", "label": "Backlog", "owner": "user"},
    ])
    store.set_project_workflow("team", "lean")
    task = store.create_task("Team task", project_id="team")

    with pytest.raises(ValueError, match="unknown status"):
        store.move_task(task.id, "uat")


def test_workflow_definition_validation(store):
    valid = [{"key": "backlog", "label": "Backlog", "owner": "user"}]

    with pytest.raises(WorkflowError):
        store.create_workflow("wf", "Wf", [])
    with pytest.raises(WorkflowError):
        store.create_workflow("wf", "Wf", [
            {"key": "backlog", "label": "A", "owner": "user"},
            {"key": "backlog", "label": "B", "owner": "user"},
        ])
    with pytest.raises(WorkflowError):
        store.create_workflow("wf", "Wf", [{"key": "Bad Key", "label": "A", "owner": "user"}])
    with pytest.raises(WorkflowError):
        store.create_workflow("wf", "Wf", [{"key": "backlog", "label": "A", "owner": "wizard"}])
    with pytest.raises(WorkflowError):
        store.create_workflow("Bad Id", "Wf", valid)

    assert [w.id for w in store.list_workflows()] == ["default"]


def test_rest_workflow_endpoints(api_client):
    client, db = api_client

    listed = client.get("/api/workflows")
    assert listed.status_code == 200
    first = listed.json()["workflows"][0]
    assert first["id"] == "default"
    assert set(first) == {"id", "name", "statuses", "settings"}
    assert set(first["statuses"][0]) == {"key", "label", "owner", "position", "active"}
    assert first["settings"]["claim_from"] == "approved"
    assert first["settings"]["claim_to"] == "analyst"
    assert first["settings"]["active_statuses"] == [
        "analyst", "in_progress", "testing",
    ]

    created = client.post("/api/workflows", json={
        "id": "lean",
        "name": "Lean",
        "statuses": [
            {"key": "backlog", "label": "Backlog", "owner": "user"},
            {"key": "done", "label": "Done", "owner": "user"},
        ],
    })
    assert created.status_code == 201

    db.create_project("proj", "Proj")
    assigned = client.put("/api/projects/proj/workflow", json={"workflow_id": "lean"})
    assert assigned.status_code == 200

    board = client.get("/api/board", params={"project": "proj"})
    assert [c["id"] for c in board.json()["columns"]] == ["backlog", "done"]

    missing_wf = client.put("/api/projects/proj/workflow", json={"workflow_id": "nope"})
    assert missing_wf.status_code == 400

    unknown_project = client.put(
        "/api/projects/ghost/workflow", json={"workflow_id": "lean"}
    )
    assert unknown_project.status_code == 404


def test_rest_board_rejects_invalid_status_with_clear_error(api_client):
    client, _ = api_client

    response = client.post("/api/tasks", json={"title": "X", "status": "warp"})

    assert response.status_code == 400
    assert "unknown status" in response.json()["detail"]


def test_mcp_columns_match_board_for_project(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", store)

    columns = mcp_server.kanban_columns("default")

    assert columns["ok"] is True
    assert [c["id"] for c in columns["data"]["columns"]] == STATUS_ORDER
    assert columns["data"]["statuses"] == STATUS_ORDER


# ---------------------------------------------------------------------------
# Workflow settings: claim + active-status semantics
# ---------------------------------------------------------------------------


def test_workflow_settings_defaults():
    settings = workflow_settings(default_workflow())

    assert settings["claim_from"] == "approved"
    assert settings["claim_to"] == "analyst"
    assert settings["active_statuses"] == ["analyst", "in_progress", "testing"]


def test_workflow_settings_fill_missing_values():
    custom = Workflow(
        id="x",
        name="X",
        statuses=(),
        settings={"claim_from": "in_progress"},
    )

    settings = workflow_settings(custom)

    assert settings["claim_from"] == "in_progress"
    assert settings["claim_to"] == "analyst"
    assert settings["active_statuses"] == ["analyst", "in_progress", "testing"]


def test_pull_task_uses_workflow_claim_settings(store):
    store.create_project("qaflow", "QA Flow")
    store.create_workflow("qaflow-wf", "QA workflow", [
        {"key": "todo", "label": "To do", "owner": "user"},
        {"key": "qa_done", "label": "QA Done", "owner": "user"},
    ])
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id='qaflow-wf'",
        ('{"claim_from": "todo", "claim_to": "qa_done"}',),
    )
    store.set_project_workflow("qaflow", "qaflow-wf")
    task = store.create_task("Claim me", status="todo", project_id="qaflow")

    pulled = store.pull_task(task.id, assignee="claude")

    assert pulled.status == "qa_done"
    assert pulled.assignee == "claude"
    history = pulled.history[-1]
    assert (history.from_status, history.to_status) == ("todo", "qa_done")


def test_pull_task_rejects_wrong_claim_status(store):
    store.create_project("qaflow", "QA Flow")
    store.create_workflow("qaflow-wf", "QA workflow", [
        {"key": "todo", "label": "To do", "owner": "user"},
        {"key": "qa_done", "label": "QA Done", "owner": "user"},
    ])
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id='qaflow-wf'",
        ('{"claim_from": "todo", "claim_to": "qa_done"}',),
    )
    store.set_project_workflow("qaflow", "qaflow-wf")
    task = store.create_task("Not claimable", status="qa_done", project_id="qaflow")

    with pytest.raises(RuntimeError, match="not 'todo'"):
        store.pull_task(task.id)


def test_default_pull_remains_approved_to_analyst(store):
    task = store.create_task("Classic")
    store.move_task(task.id, "approved")

    pulled = store.pull_task(task.id)

    assert pulled.status == "analyst"
    history = pulled.history[-1]
    assert (history.from_status, history.to_status) == ("approved", "analyst")


def test_mcp_my_active_uses_workflow_active_statuses(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", store)
    store.create_project("qaflow", "QA Flow")
    store.create_workflow("qaflow-wf", "QA workflow", [
        {"key": "todo", "label": "To do", "owner": "user"},
        {"key": "qa_done", "label": "QA Done", "owner": "user"},
    ])
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id='qaflow-wf'",
        ('{"active_statuses": ["qa_done"]}',),
    )
    store.set_project_workflow("qaflow", "qaflow-wf")
    active_task = store.create_task(
        "Working", status="qa_done", assignee="claude", project_id="qaflow"
    )
    store.create_task("Idle", status="todo", project_id="qaflow")

    result = mcp_server.kanban_my_active(project_id="qaflow")

    assert result["ok"] is True
    ids = [t["id"] for t in result["data"]["tasks"]]
    assert ids == [active_task.id]


def test_mcp_my_active_default_workflow_unchanged(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", store)
    task = store.create_task("One", assignee="claude")
    store.move_task(task.id, "testing")
    store.create_task("Backlogged", assignee="claude")

    result = mcp_server.kanban_my_active()

    assert result["ok"] is True
    assert [t["id"] for t in result["data"]["tasks"]] == ["T-001"]
