"""Transition enforcement (Phase 5): opt-in workflow owner checks.

Workflow statuses declare an owner (user | agent | any). Enforcement is
opt-in per workflow via the ``enforce_owners`` setting (default off, so
existing behavior is byte-identical until enabled). When enabled, an actor
classified as "agent" cannot move a task into a user-owned status and vice
versa; "any" accepts both. Rejections surface as ValueError (REST 400 /
MCP error payload / rule-engine logged failure).
"""
from __future__ import annotations

import pytest

from kanban_store import Store
from kanban_store.workflows import (
    Workflow,
    actor_kind,
    actor_may_enter,
    default_workflow,
    transition_allowed,
    workflow_settings,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    s = Store(tmp_path / "enforce.db")
    yield s
    s.close()


def enable_enforcement(store: Store, workflow_id: str = "default") -> None:
    """Opt the workflow into owner enforcement (settings API is a later block)."""
    row = store._conn.execute(
        "SELECT settings_json FROM workflows WHERE id=?", (workflow_id,)
    ).fetchone()
    import json

    settings = json.loads(row["settings_json"])
    settings["enforce_owners"] = True
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id=?",
        (json.dumps(settings), workflow_id),
    )


# ---------------------------------------------------------------------------
# Cycle 1: pure helpers
# ---------------------------------------------------------------------------


def test_actor_kind_classifies_agents():
    assert actor_kind("automation") == "agent"
    assert actor_kind("claude") == "agent"
    assert actor_kind("claude-opus-4") == "agent"
    assert actor_kind("agent:builder") == "agent"
    assert actor_kind("agent") == "agent"


def test_actor_kind_defaults_to_user():
    assert actor_kind("user") == "user"
    assert actor_kind("michs") == "user"
    assert actor_kind("") == "user"


def test_workflow_settings_include_enforce_owners_default_off():
    settings = workflow_settings(default_workflow())

    assert settings["enforce_owners"] is False


def test_actor_may_enter_when_enforcement_off_allows_everything():
    workflow = default_workflow()

    assert actor_may_enter(workflow, "uat", "claude") is True
    assert actor_may_enter(workflow, "analyst", "user") is True
    assert actor_may_enter(workflow, "done", "automation") is True


def test_actor_may_enter_enforced_split():
    workflow = default_workflow()
    settings = dict(workflow_settings(workflow))
    settings["enforce_owners"] = True
    from kanban_store.workflows import Workflow

    enforced = Workflow(
        id=workflow.id,
        name=workflow.name,
        statuses=workflow.statuses,
        settings=settings,
    )

    # user-owned columns reject agents; agent-owned reject users
    assert actor_may_enter(enforced, "uat", "user") is True
    assert actor_may_enter(enforced, "uat", "claude") is False
    assert actor_may_enter(enforced, "analyst", "claude") is True
    assert actor_may_enter(enforced, "analyst", "user") is False
    # any accepts both
    assert actor_may_enter(enforced, "blocked", "claude") is True
    assert actor_may_enter(enforced, "blocked", "user") is True
    # automation counts as agent
    assert actor_may_enter(enforced, "done", "automation") is False
    # unknown status: the caller's validation reports it; deny here
    assert actor_may_enter(enforced, "nope", "user") is False


# ---------------------------------------------------------------------------
# Transition graphs (Phase 5b): optional per-status allowed-next lists
# ---------------------------------------------------------------------------


def graphed_workflow(store_unused: None = None, **settings_overrides) -> Workflow:
    settings = {
        "transitions": {
            "backlog": ["approved", "blocked"],
            "approved": ["analyst"],
        }
    }
    settings.update(settings_overrides)
    base = default_workflow()
    return Workflow(
        id=base.id, name=base.name, statuses=base.statuses, settings=settings
    )


def test_transition_allowed_without_setting_is_open():
    workflow = default_workflow()

    assert transition_allowed(workflow, "backlog", "done") is True


def test_transition_allowed_respects_graph():
    workflow = graphed_workflow()

    assert transition_allowed(workflow, "backlog", "approved") is True
    assert transition_allowed(workflow, "backlog", "blocked") is True
    assert transition_allowed(workflow, "backlog", "done") is False
    assert transition_allowed(workflow, "approved", "analyst") is True
    assert transition_allowed(workflow, "approved", "done") is False


def test_transition_allowed_statuses_absent_from_graph_are_open():
    workflow = graphed_workflow()

    # 'uat' has no entry: transitions out of it are unrestricted
    assert transition_allowed(workflow, "uat", "done") is True
    assert transition_allowed(workflow, "uat", "backlog") is True


def test_transition_allowed_ignores_malformed_entries():
    workflow = graphed_workflow(**{"transitions": {"backlog": "not-a-list"}})

    assert transition_allowed(workflow, "backlog", "done") is True

    workflow = graphed_workflow(**{"transitions": "garbage"})

    assert transition_allowed(workflow, "backlog", "done") is True


# ---------------------------------------------------------------------------
# Cycle 2: Store enforcement (moves + claims)
# ---------------------------------------------------------------------------


def test_move_task_enforcement_off_preserves_current_behavior(store):
    task = store.create_task("Legacy flow", actor="claude")

    # today any actor may enter any status; this must not change by default
    moved = store.move_task(task.id, "uat", actor="claude")

    assert moved.status == "uat"


def test_move_task_enforced_rejects_agent_into_user_column(store):
    enable_enforcement(store)
    task = store.create_task("Governed", actor="user")

    with pytest.raises(ValueError, match="uat"):
        store.move_task(task.id, "uat", actor="claude")

    # nothing changed: no move, no history, no outbox move-event
    assert store.get_task(task.id).status == "backlog"
    history = store.get_task(task.id).history
    assert all(h.action != "move" for h in history)
    assert [
        e for e in store.list_pending_events() if e["event_type"] == "task_moved"
    ] == []


def test_move_task_enforced_rejects_user_into_agent_column(store):
    enable_enforcement(store)
    task = store.create_task("Governed", actor="user")

    with pytest.raises(ValueError, match="analyst"):
        store.move_task(task.id, "analyst", actor="michs")

    assert store.get_task(task.id).status == "backlog"


def test_move_task_enforced_permits_owner_matches(store):
    enable_enforcement(store)
    task = store.create_task("Governed", actor="user")

    # user-owned column accepts the user
    assert store.move_task(task.id, "uat", actor="michs").status == "uat"
    # agent-owned column accepts the agent
    claimed = store.move_task(task.id, "in_progress", actor="agent:builder")

    assert claimed.status == "in_progress"


def test_pull_task_enforced_rejects_user_owned_claim_target(store):
    store.create_project("humanflow", "Human flow")
    store.create_workflow("human-wf", "Human workflow", [
        {"key": "todo", "label": "To do", "owner": "user"},
        {"key": "review", "label": "Review", "owner": "user"},
    ])
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id='human-wf'",
        ('{"claim_from": "todo", "claim_to": "review", "enforce_owners": true}',),
    )
    store.set_project_workflow("humanflow", "human-wf")
    task = store.create_task("Human claim", status="todo", project_id="humanflow")

    with pytest.raises(ValueError, match="review"):
        store.pull_task(task.id, assignee="claude")

    assert store.get_task(task.id).status == "todo"
    assert store.get_task(task.id).assignee is None


def test_pull_task_enforced_allows_agent_claim_target(store):
    task = store.create_task("Normal claim", actor="user")
    store.move_task(task.id, "approved", actor="user")
    enable_enforcement(store)

    pulled = store.pull_task(task.id, assignee="claude")

    assert pulled.status == "analyst"
    assert pulled.assignee == "claude"


def test_rule_action_into_forbidden_column_is_logged_not_fatal(store, monkeypatch):
    from kanban_ui.automation import rules
    from kanban_ui.automation.rules import RuleEngine

    import json

    rules_file = store.db_path.parent / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "Auto approve",
                        "enabled": True,
                        "trigger": {"type": "task_moved", "to_status": "approved"},
                        "action": {"type": "move_to", "status": "uat"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "_engine", None)
    engine = RuleEngine(store, rules_file, interval=9999)
    engine._maybe_reload()
    enable_enforcement(store)
    task = store.create_task("Rule target", actor="user")
    store.move_task(task.id, "approved", actor="claude")  # agent-owned column

    rules.emit_rule_event("task_moved", {
        "task": store.get_task(task.id).to_public(),
        "project": store.get_project("default").to_public(),
        "from_status": "backlog",
        "to_status": "approved",
        "comment": None,
    })

    # the move was rejected and logged; the task stays put, engine healthy
    assert store.get_task(task.id).status == "approved"
    errors = rules.rules_status()["last_errors"]
    assert any("uat" in e["error"] for e in errors)


# ---------------------------------------------------------------------------
# Cycle 3: REST + MCP surfaces
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from kanban_ui import main

    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db = Store(tmp_path / "api-enforce.db")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    monkeypatch.setenv("KANBAN_EVENT_POLL_INTERVAL", "9999")
    enable_enforcement(db)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


def test_rest_move_rejected_with_400_for_agent_into_user_column(
    api_client, monkeypatch
):
    client, db = api_client
    monkeypatch.setenv("KANBAN_ACTOR", "claude")
    task_id = client.post(
        "/api/tasks", json={"title": "Agent via REST", "project_id": "default"}
    ).json()["id"]

    response = client.post(f"/api/tasks/{task_id}/move", json={"to_status": "uat"})

    assert response.status_code == 400
    assert "uat" in response.json()["detail"]


def test_rest_move_allows_owner_matches(api_client, monkeypatch):
    client, db = api_client
    monkeypatch.setenv("KANBAN_ACTOR", "michs")
    task_id = client.post(
        "/api/tasks", json={"title": "User via REST", "project_id": "default"}
    ).json()["id"]

    response = client.post(f"/api/tasks/{task_id}/move", json={"to_status": "uat"})

    assert response.status_code == 200
    assert response.json()["status"] == "uat"


def test_mcp_move_reports_enforcement_error(store, monkeypatch):
    from kanban_mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_store", store)
    enable_enforcement(store)
    task = store.create_task("MCP governed", actor="user")

    result = mcp_server.kanban_move(task.id, "uat")

    assert result["ok"] is False
    assert "uat" in result["error"]
    assert store.get_task(task.id).status == "backlog"


# ---------------------------------------------------------------------------
# Graph wiring (Phase 5b): Store + REST
# ---------------------------------------------------------------------------


def enable_graph(store: Store, workflow_id: str = "default") -> None:
    import json

    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "transitions": {
                        "backlog": ["approved", "blocked"],
                        "approved": ["analyst"],
                    }
                }
            ),
            workflow_id,
        ),
    )


def test_move_task_graph_blocks_skipped_statuses(store):
    enable_graph(store)
    task = store.create_task("Graphed", actor="user")

    with pytest.raises(ValueError, match="transition"):
        store.move_task(task.id, "done", actor="user")

    assert store.get_task(task.id).status == "backlog"
    assert [
        e for e in store.list_pending_events() if e["event_type"] == "task_moved"
    ] == []


def test_move_task_graph_allows_listed_transitions(store):
    enable_graph(store)
    task = store.create_task("Graphed", actor="user")

    assert store.move_task(task.id, "blocked", actor="user").status == "blocked"
    back = store.move_task(task.id, "backlog", actor="user")  # unlisted: open

    assert back.status == "backlog"
    assert store.move_task(task.id, "approved", actor="claude").status == "approved"


def test_pull_task_respects_graph(store):
    enable_graph(store)
    task = store.create_task("Claim path", actor="user")
    store.move_task(task.id, "approved", actor="claude")

    # graph: approved → analyst only, and the default claim is approved → analyst
    pulled = store.pull_task(task.id, assignee="claude")

    assert pulled.status == "analyst"


def test_pull_task_graph_blocks_undesignated_claim(store):
    store._conn.execute(
        "UPDATE workflows SET settings_json=? WHERE id='default'",
        (
            '{"claim_from": "approved", "claim_to": "done", '
            '"transitions": {"approved": ["analyst"]}}',
        ),
    )
    task = store.create_task("Wrong claim", actor="user")
    store.move_task(task.id, "approved", actor="claude")

    with pytest.raises(ValueError, match="transition"):
        store.pull_task(task.id, assignee="claude")

    assert store.get_task(task.id).status == "approved"


def test_rest_move_reports_graph_rejection(api_client, monkeypatch):
    client, db = api_client
    enable_graph(db)
    monkeypatch.setenv("KANBAN_ACTOR", "michs")
    task_id = client.post(
        "/api/tasks", json={"title": "Graphed REST", "project_id": "default"}
    ).json()["id"]

    response = client.post(f"/api/tasks/{task_id}/move", json={"to_status": "done"})

    assert response.status_code == 400
    assert "transition" in response.json()["detail"]
