"""Durable event outbox (Phase 3.1).

Every committed mutation from any client records an equivalent event row in
the same transaction. Mutations performed by automation (actor="automation")
and PLAN.md imports are suppressed: rule-triggered mutations must not recurse
and bulk imports must not storm webhooks.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kanban_store import Store
from kanban_store.events import EVENT_TYPES
from kanban_store.migrations import LATEST_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    s = Store(tmp_path / "outbox.db")
    yield s
    s.close()


def events(store: Store, event_type: str | None = None) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT * FROM issue_events ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        if event_type is not None and row["event_type"] != event_type:
            continue
        result.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "task_id": row["task_id"],
                "project_id": row["project_id"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
                "delivered_at": row["delivered_at"],
            }
        )
    return result


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_outbox_schema_and_version(store):
    assert LATEST_VERSION == 8
    assert store.schema_version() == 8
    tables = {
        r["name"]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "issue_events" in tables


def test_schema_reopen_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    Store(tmp_path / "reopen.db").close()
    store = Store(tmp_path / "reopen.db")
    assert store.schema_version() == 8


# ---------------------------------------------------------------------------
# Event types + payload shapes (match today's webhook payloads)
# ---------------------------------------------------------------------------


def test_event_types_catalog():
    assert EVENT_TYPES == frozenset(
        {"task_created", "task_moved", "task_updated", "task_commented"}
    )


def test_create_task_records_task_created(store):
    store.create_task("Ship it", actor="claude")

    created = events(store, "task_created")
    assert len(created) == 1
    assert created[0]["task_id"] == "T-001"
    assert created[0]["project_id"] == "default"
    assert created[0]["actor"] == "claude"
    assert created[0]["delivered_at"] is None
    payload = created[0]["payload"]
    assert set(payload) == {"task", "project"}
    assert payload["task"]["id"] == "T-001"
    assert payload["project"]["id"] == "default"


def test_move_task_records_task_moved(store):
    task = store.create_task("Move me", actor="claude")

    store.move_task(task.id, "approved", actor="claude", comment="go")

    moved = events(store, "task_moved")
    assert len(moved) == 1
    payload = moved[0]["payload"]
    assert set(payload) == {"task", "project", "from_status", "to_status", "comment"}
    assert payload["from_status"] == "backlog"
    assert payload["to_status"] == "approved"
    assert payload["comment"] == "go"


def test_same_status_move_records_nothing(store):
    task = store.create_task("Reorder only", actor="claude")

    store.move_task(task.id, "backlog", actor="claude")

    assert events(store, "task_moved") == []
    assert len(events(store)) == 1  # only the create event


def test_update_fields_records_task_updated(store):
    task = store.create_task("Edit me", actor="claude")

    store.update_fields(task.id, actor="claude", priority="high", labels=["ops"])

    updated = events(store, "task_updated")
    assert len(updated) == 1
    payload = updated[0]["payload"]
    assert set(payload) == {"task", "project", "changed_fields"}
    assert payload["changed_fields"] == ["priority", "labels"]


def test_no_update_event_without_changes(store):
    task = store.create_task("Noop", actor="claude")
    store.update_fields(task.id, actor="claude")  # no fields -> early return

    assert events(store, "task_updated") == []


def test_add_comment_records_task_commented(store):
    task = store.create_task("Comment me", actor="claude")

    store.add_comment(task.id, "looks good", actor="claude")

    commented = events(store, "task_commented")
    assert len(commented) == 1
    payload = commented[0]["payload"]
    assert set(payload) == {"task", "project", "comment"}
    assert payload["comment"] == "looks good"


def test_pull_task_records_task_moved(store):
    task = store.create_task("Claim me", actor="user")
    store.move_task(task.id, "approved", actor="user")

    store.pull_task(task.id, assignee="claude")

    moved = [e for e in events(store, "task_moved") if e["actor"] == "claude"]
    assert len(moved) == 1
    assert moved[0]["payload"]["from_status"] == "approved"
    assert moved[0]["payload"]["to_status"] == "analyst"


# ---------------------------------------------------------------------------
# Suppression: automation actor + explicit context manager
# ---------------------------------------------------------------------------


def test_automation_mutations_record_no_events(store):
    task = store.create_task("Auto", actor="automation")
    store.move_task(task.id, "done", actor="automation")
    store.update_fields(task.id, actor="automation", priority="high")
    store.add_comment(task.id, "auto comment", actor="automation")

    assert events(store) == []


def test_events_suppressed_context_manager(store):
    task = store.create_task("Bulk", actor="plan-import")

    with store.events_suppressed():
        store.create_task("Imported 1", actor="plan-import")
        store.move_task(task.id, "approved", actor="plan-import")

    types = [e["event_type"] for e in events(store)]
    assert types == ["task_created"]  # only the pre-suppression create


def test_suppression_is_not_sticky(store):
    with store.events_suppressed():
        pass

    store.create_task("Still recorded", actor="claude")

    assert len(events(store, "task_created")) == 1


# ---------------------------------------------------------------------------
# Delivery bookkeeping
# ---------------------------------------------------------------------------


def test_list_pending_and_mark_delivered(store):
    store.create_task("One", actor="claude")
    store.create_task("Two", actor="claude")

    pending = store.list_pending_events()
    assert [p["event_type"] for p in pending] == ["task_created", "task_created"]
    assert all(p["delivered_at"] is None for p in pending)

    store.mark_event_delivered(pending[0]["id"])

    remaining = store.list_pending_events()
    assert len(remaining) == 1
    assert remaining[0]["payload"]["task"]["id"] == "T-002"
    # the marked row keeps its data, only delivered_at flips
    done = events(store)
    assert sum(1 for e in done if e["delivered_at"] is not None) == 1


def test_failed_mutation_records_no_event(store):
    with pytest.raises(ValueError):
        store.create_task("Ghost", project_id="missing-project")

    assert events(store) == []


# ---------------------------------------------------------------------------
# Dispatcher: delivers pending events exactly once
# ---------------------------------------------------------------------------


def test_dispatcher_delivers_pending_events(store):
    from kanban_ui.automation.event_dispatcher import EventDispatcher

    delivered: list[tuple[str, dict[str, Any]]] = []
    reactive: list[tuple[str, dict[str, Any]]] = []
    dispatcher = EventDispatcher(
        store,
        emit_webhook=lambda e, p: delivered.append((e, p)),
        apply_reactive=lambda e, p: reactive.append((e, p)),
    )
    task = store.create_task("One", actor="claude")
    store.move_task(task.id, "approved", actor="claude", comment="go")
    assert len(store.list_pending_events()) == 2

    processed = asyncio.run(dispatcher.process_pending())

    assert processed == 2
    assert [e for e, _ in delivered] == ["task_created", "task_moved"]
    assert [e for e, _ in reactive] == ["task_moved"]
    moved_payload = delivered[1][1]
    assert moved_payload["from_status"] == "backlog"
    assert moved_payload["to_status"] == "approved"
    assert store.list_pending_events() == []

    # already-delivered events are not re-delivered
    assert asyncio.run(dispatcher.process_pending()) == 0
    assert len(delivered) == 2


def test_dispatcher_marks_delivered_on_emit_failure(store):
    from kanban_ui.automation.event_dispatcher import EventDispatcher

    def boom(event: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("webhook down")

    dispatcher = EventDispatcher(store, emit_webhook=boom)
    store.create_task("X", actor="claude")

    processed = asyncio.run(dispatcher.process_pending())

    # single-attempt delivery: failures are logged, events not retried
    assert processed == 1
    assert store.list_pending_events() == []


def test_dispatcher_marks_unknown_event_types_delivered(store, tmp_path):
    from kanban_ui.automation.event_dispatcher import EventDispatcher

    delivered: list[tuple[str, dict[str, Any]]] = []
    dispatcher = EventDispatcher(
        store, emit_webhook=lambda e, p: delivered.append((e, p))
    )
    store._conn.execute(
        """INSERT INTO issue_events
           (event_type, task_id, project_id, actor, payload_json, created_at)
           VALUES ('custom_future_event', 'T-001', 'default', 'user', '{}', '2026-01-01')"""
    )

    processed = asyncio.run(dispatcher.process_pending())

    assert processed == 1
    assert delivered == []  # unknown types are consumed, not fanned out
    assert store.list_pending_events() == []


# ---------------------------------------------------------------------------
# REST parity + lifespan wiring (dispatcher drains the outbox)
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    from kanban_ui import main

    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db = Store(tmp_path / "api-events.db")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    monkeypatch.setenv("KANBAN_EVENT_POLL_INTERVAL", "0.05")
    from fastapi.testclient import TestClient

    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_rest_mutations_record_outbox_events(api_client):
    client, db = api_client

    created = client.post(
        "/api/tasks",
        json={"title": "REST flow", "project_id": "default"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    client.post(
        f"/api/tasks/{task_id}/move",
        json={"to_status": "approved", "comment": "ship it"},
    )
    client.patch(f"/api/tasks/{task_id}", json={"priority": "high"})
    client.post(f"/api/tasks/{task_id}/comment", json={"text": "nice"})

    # the lifespan dispatcher may drain pending rows concurrently, so read
    # all recorded events instead of only the pending ones
    types = [e["event_type"] for e in events(db)]
    assert types == [
        "task_created", "task_moved", "task_updated", "task_commented",
    ]
    moved = next(e for e in events(db) if e["event_type"] == "task_moved")
    assert moved["payload"]["from_status"] == "backlog"
    assert moved["payload"]["to_status"] == "approved"
    assert moved["payload"]["comment"] == "ship it"


def test_dispatcher_wiring_drains_pending_events(api_client):
    client, db = api_client

    client.post("/api/tasks", json={"title": "Wired", "project_id": "default"})

    assert _wait_until(lambda: db.list_pending_events() == []), (
        "lifespan dispatcher did not drain the outbox"
    )
    done = db._conn.execute(
        "SELECT COUNT(*) AS n FROM issue_events WHERE delivered_at IS NOT NULL"
    ).fetchone()
    assert done["n"] == 1


def test_mcp_style_mutation_triggers_reactive_rule(api_client):
    from kanban_ui.automation import rules

    client, db = api_client
    (db.db_path.parent / "rules.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "Approved ships",
                        "enabled": True,
                        "trigger": {"type": "task_moved", "to_status": "approved"},
                        "action": {"type": "move_to", "status": "done"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # Simulate an MCP/stdio-side mutation: a direct Store call the REST layer
    # never sees. Pre-outbox this produced no automation at all.
    task = db.create_task("Via MCP", actor="claude")
    db.move_task(task.id, "approved", actor="claude")

    assert _wait_until(
        lambda: len(rules.rules_status()["last_reactive"]) == 1
    ), "reactive rule did not fire for a non-REST mutation"
    assert db.get_task(task.id).status == "done"


def test_automation_status_includes_events(api_client):
    client, _db = api_client

    response = client.get("/api/automation/status")

    assert response.status_code == 200
    body = response.json()
    assert "events" in body
    assert body["events"]["running"] is True


def test_dispatcher_rules_do_not_recurse(store, tmp_path, monkeypatch):
    from kanban_ui.automation import rules
    from kanban_ui.automation.event_dispatcher import EventDispatcher
    from kanban_ui.automation.rules import RuleEngine

    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "Approved ships",
                        "enabled": True,
                        "trigger": {"type": "task_moved", "to_status": "approved"},
                        "action": {"type": "move_to", "status": "done"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "_engine", None)
    engine = RuleEngine(store, rules_file, interval=9999)
    engine._maybe_reload()

    dispatcher = EventDispatcher(store)
    task = store.create_task("Auto ship", actor="claude")
    store.move_task(task.id, "approved", actor="claude")
    assert len(store.list_pending_events()) == 2

    processed = asyncio.run(dispatcher.process_pending())

    # rules fired (task is done) but the automation move created no new event
    assert processed == 2
    assert store.get_task(task.id).status == "done"
    reactive = rules.rules_status()["last_reactive"]  # module-global: filter by task
    assert any(e["task_id"] == task.id for e in reactive)
    assert store.list_pending_events() == []
