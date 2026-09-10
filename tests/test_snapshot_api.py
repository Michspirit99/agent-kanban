from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kanban_store import Store
from kanban_ui import main


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    db = Store(tmp_path / "api.db")
    # Import tests use a genuinely empty target rather than the bootstrap row.
    db._conn.execute("DELETE FROM projects WHERE id='default'")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


@pytest.fixture
def snapshot(tmp_path):
    source = Store(tmp_path / "source.db")
    try:
        source._conn.execute("DELETE FROM projects WHERE id='default'")
        project = source.create_project("imported", "Imported")
        source.create_task("Imported task", project_id=project.id)
        return source.snapshot()
    finally:
        source.close()


def test_snapshot_import_is_described_in_openapi():
    schema = main.app.openapi()

    operation = schema["paths"]["/api/snapshot/import"]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SnapshotImportRequest"
    }


def test_import_snapshot_returns_report_and_restores_data(api_client, snapshot):
    client, db = api_client

    response = client.post("/api/snapshot/import", json={"snapshot": snapshot})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "imported": {
            "projects": 1,
            "tasks": 1,
            "links": 0,
            "blockers": 0,
            "history": 1,
        },
    }
    assert db.get_project("imported") is not None
    assert db.get_task("T-001") is not None


def test_repeated_import_is_idempotent(api_client, snapshot):
    client, _ = api_client

    first = client.post("/api/snapshot/import", json={"snapshot": snapshot})
    second = client.post("/api/snapshot/import", json={"snapshot": snapshot})

    assert first.status_code == second.status_code == 200
    assert second.json() == {
        "ok": True,
        "imported": {
            "projects": 0,
            "tasks": 0,
            "links": 0,
            "blockers": 0,
            "history": 0,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("projects"),
        lambda payload: payload.update(snapshot_version=2),
    ],
    ids=["malformed", "future-version"],
)
def test_invalid_import_returns_400_without_mutation(api_client, snapshot, mutate):
    client, db = api_client
    before = db.snapshot()
    mutate(snapshot)

    response = client.post("/api/snapshot/import", json={"snapshot": snapshot})

    assert response.status_code == 400
    assert "Traceback" not in response.text
    after = db.snapshot()
    assert after["projects"] == before["projects"]
    assert after["tasks"] == before["tasks"]


def test_non_object_snapshot_returns_400_without_mutation(api_client):
    client, db = api_client
    before = db.snapshot()

    response = client.post("/api/snapshot/import", json={"snapshot": []})

    assert response.status_code == 400
    assert db.snapshot()["projects"] == before["projects"]
    assert db.snapshot()["tasks"] == before["tasks"]


def test_conflicting_import_returns_409_without_partial_mutation(api_client, snapshot):
    client, db = api_client
    db.create_project("imported", "Different project")
    before = db.snapshot()

    response = client.post("/api/snapshot/import", json={"snapshot": snapshot})

    assert response.status_code == 409
    assert "Traceback" not in response.text
    after = db.snapshot()
    assert after["projects"] == before["projects"]
    assert after["tasks"] == before["tasks"]
    assert db.get_task("T-001") is None
