from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store import Store
from kanban_ui import main


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "tasks.db")
    yield db
    db.close()


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


def test_add_link_requires_an_existing_task(store):
    with pytest.raises(KeyError):
        store.add_link("T-999", "url", "https://example.com")


def test_set_blockers_requires_an_existing_task(store):
    with pytest.raises(KeyError):
        store.set_blockers("T-999", [])


def test_missing_blocker_preserves_existing_blockers(store):
    task = store.create_task("Task")
    blocker = store.create_task("Blocker")
    store.set_blockers(task.id, [blocker.id])

    with pytest.raises(ValueError):
        store.set_blockers(task.id, ["T-999"])

    assert store.get_task(task.id).blockers == [blocker.id]


def test_task_cannot_block_itself(store):
    task = store.create_task("Task")

    with pytest.raises(ValueError):
        store.set_blockers(task.id, [task.id])


def test_api_returns_404_for_link_on_missing_task(api_client):
    client, _ = api_client

    response = client.post(
        "/api/tasks/T-999/links",
        json={"type": "url", "value": "https://example.com"},
    )

    assert response.status_code == 404


def test_api_returns_404_for_blockers_on_missing_task(api_client):
    client, _ = api_client

    response = client.post(
        "/api/tasks/T-999/blockers",
        json={"blocker_ids": []},
    )

    assert response.status_code == 404


def test_api_returns_400_for_missing_blocker_and_preserves_existing(api_client):
    client, db = api_client
    task = db.create_task("Task")
    blocker = db.create_task("Blocker")
    db.set_blockers(task.id, [blocker.id])

    response = client.post(
        f"/api/tasks/{task.id}/blockers",
        json={"blocker_ids": ["T-999"]},
    )

    assert response.status_code == 400
    assert db.get_task(task.id).blockers == [blocker.id]


def test_api_returns_400_for_self_blocker(api_client):
    client, db = api_client
    task = db.create_task("Task")

    response = client.post(
        f"/api/tasks/{task.id}/blockers",
        json={"blocker_ids": [task.id]},
    )

    assert response.status_code == 400
