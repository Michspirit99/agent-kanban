"""Indexed search (Phase 4.1): FTS5-backed Store.search with substring fallback.

The existing MCP ``kanban_search`` keeps its substring-over-title/description
contract; this slice adds a central ``Store.search`` with two modes, an FTS5
index kept in sync by triggers, a REST ``/api/search`` endpoint, and graceful
degradation when SQLite lacks FTS5.
"""
from __future__ import annotations


import pytest

from kanban_store import Store
from kanban_store.migrations import LATEST_VERSION
from kanban_store.searching import build_fts_query


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    s = Store(tmp_path / "search.db")
    yield s
    s.close()


def seed(store: Store) -> None:
    store.create_task(
        "Database migration",
        description="Move storage to SQLite",
        acceptance="schema v8 applies cleanly",
        actor="user",
    )
    store.create_task("Landing page", description="hero + CTA", actor="user")


# ---------------------------------------------------------------------------
# Cycle 1: pure sanitizer + substring baseline
# ---------------------------------------------------------------------------


def test_build_fts_query_prefixes_and_escapes_tokens():
    assert build_fts_query("data fix") == '"data"* "fix"*'


def test_build_fts_query_escapes_embedded_quotes():
    # FTS5 escapes quotes by doubling them, not with backslashes
    assert build_fts_query('say "hi"') == '"say"* """hi"""*'


def test_search_substring_matches_all_text_fields(store):
    seed(store)

    by_title = store.search("database", mode="substring")
    by_description = store.search("storage", mode="substring")
    by_acceptance = store.search("applies", mode="substring")

    assert [t.id for t in by_title] == ["T-001"]
    assert [t.id for t in by_description] == ["T-001"]
    assert [t.id for t in by_acceptance] == ["T-001"]


def test_search_substring_case_insensitive(store):
    seed(store)

    assert [t.id for t in store.search("DATABASE", mode="substring")] == ["T-001"]


def test_search_project_filter(store):
    seed(store)
    store.create_project("web", "Web")
    store.create_task("Database migration", project_id="web", actor="user")

    hits = store.search("database", project_id="web", mode="substring")

    assert [t.id for t in hits] == ["T-003"]


def test_search_rejects_short_query(store):
    with pytest.raises(ValueError, match="at least 2 characters"):
        store.search("d")


def test_search_rejects_unknown_mode(store):
    with pytest.raises(ValueError, match="mode"):
        store.search("database", mode="regex")


# ---------------------------------------------------------------------------
# Cycle 2: FTS5 mode + migration v8
# ---------------------------------------------------------------------------


def test_schema_version_8(store):
    assert LATEST_VERSION == 8
    assert store.schema_version() == 8


def test_search_fts_token_prefix_matches(store):
    seed(store)

    assert [t.id for t in store.search("data")] == ["T-001"]


def test_search_fts_matches_acceptance(store):
    seed(store)

    assert [t.id for t in store.search("cleanly")] == ["T-001"]


def test_search_fts_multi_token_is_and(store):
    seed(store)

    assert [t.id for t in store.search("database storage")] == ["T-001"]
    assert store.search("database hero") == []


def test_search_fts_reflects_updates(store):
    seed(store)
    store.update_fields("T-001", actor="user", title="Auth rewrite")

    assert [t.id for t in store.search("rewrite")] == ["T-001"]
    assert store.search("migration") == []


def test_search_fts_reflects_snapshot_style_raw_insert(store):
    seed(store)
    store._conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-999', 'Inbox capture', 'backlog', 'normal', 'M', NULL,
                   'from raw SQL', '', NULL, '2026-01-01', '2026-01-01', 0,
                   'default', 'task', NULL, '[]', '{}', '2026-01-01')"""
    )

    assert [t.id for t in store.search("capture")] == ["T-999"]


def test_search_fts_project_filter(store):
    seed(store)
    store.create_project("web", "Web")
    store.create_task("Database migration", project_id="web", actor="user")

    assert [t.id for t in store.search("database", project_id="web")] == ["T-003"]


def test_migration_v8_backfills_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")

    import sqlite3

    from kanban_store.migrations import MIGRATIONS, apply_migrations

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    # stop at v7 (no FTS table, no triggers), insert a task, then upgrade
    apply_migrations(conn, migrations=MIGRATIONS[:-1])
    conn.execute(
        """INSERT INTO tasks (id, title, status, priority, size, assignee,
                               description, acceptance, external_blocker,
                               created_at, moved_at, column_order, project_id,
                               issue_type, reporter, labels_json,
                               custom_fields_json, updated_at)
           VALUES ('T-001', 'Legacy haystack', 'backlog', 'normal', 'M', NULL,
                   'written before FTS', 'needle in acceptance', NULL,
                   '2026-01-01', '2026-01-01', 0, 'default', 'task', NULL,
                   '[]', '{}', '2026-01-01')"""
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        assert store.schema_version() == 8
        assert [t.id for t in store.search("haystack")] == ["T-001"]
        assert [t.id for t in store.search("needle")] == ["T-001"]
    finally:
        store.close()


def test_fts_unavailable_falls_back_to_substring(store):
    seed(store)
    store._conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('fts5', 'off')"
    )

    assert store.fts_available is False
    assert [t.id for t in store.search("database")] == ["T-001"]


# ---------------------------------------------------------------------------
# Cycle 3: REST endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from kanban_ui import main

    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    db = Store(tmp_path / "api-search.db")
    monkeypatch.setattr(main, "_store", db)
    monkeypatch.setenv("KANBAN_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("KANBAN_RULES_FILE", str(tmp_path / "rules.json"))
    monkeypatch.setenv("KANBAN_WEBHOOKS_FILE", str(tmp_path / "webhooks.json"))
    monkeypatch.setenv("KANBAN_EVENT_POLL_INTERVAL", "9999")
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield client, db
    db.close()


def test_rest_search_endpoint(api_client):
    client, _db = api_client
    client.post(
        "/api/tasks",
        json={
            "title": "Database migration",
            "acceptance": "schema v8 applies cleanly",
            "project_id": "default",
        },
    )

    response = client.get("/api/search", params={"q": "database"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["tasks"][0]["title"] == "Database migration"
    assert body["mode"] == "fts"


def test_rest_search_finds_acceptance_text(api_client):
    client, _db = api_client
    client.post(
        "/api/tasks",
        json={
            "title": "Unrelated title",
            "acceptance": "the collector must drain",
            "project_id": "default",
        },
    )

    response = client.get("/api/search", params={"q": "collector"})

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_rest_search_short_query_is_400(api_client):
    client, _db = api_client

    response = client.get("/api/search", params={"q": "d"})

    assert response.status_code == 400


def test_rest_search_invalid_mode_is_400(api_client):
    client, _db = api_client

    response = client.get("/api/search", params={"q": "database", "mode": "regex"})

    assert response.status_code == 400


def test_rest_search_project_filter(api_client):
    client, db = api_client
    db.create_project("web", "Web")
    client.post("/api/tasks", json={"title": "Searchable", "project_id": "default"})
    client.post("/api/tasks", json={"title": "Searchable too", "project_id": "web"})

    response = client.get(
        "/api/search", params={"q": "searchable", "project": "web"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["tasks"][0]["project_id"] == "web"
