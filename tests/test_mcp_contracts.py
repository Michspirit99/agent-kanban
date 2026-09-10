"""Characterization tests: freeze current stdio MCP tool contracts."""
from __future__ import annotations

import asyncio

import pytest

from kanban_mcp import server as mcp_server
from kanban_store import Store


EXPECTED_TOOLS = {
    "kanban_columns", "kanban_list", "kanban_projects", "kanban_board",
    "kanban_search", "kanban_my_active", "kanban_get", "kanban_pull",
    "kanban_move", "kanban_comment", "kanban_create", "kanban_link",
    "kanban_blockers", "kanban_update",
}


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_ID", "default")
    monkeypatch.setenv("KANBAN_DEFAULT_PROJECT_NAME", "Default")
    value = Store(tmp_path / "mcp.db")
    monkeypatch.setattr(mcp_server, "_store", value)
    yield value
    value.close()


def test_all_tools_are_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 14


def test_envelope_shapes(store):
    columns = mcp_server.kanban_columns()

    assert set(columns) == {"ok", "data"}
    assert columns["ok"] is True
    assert set(columns["data"]) == {"columns", "statuses"}

    missing = mcp_server.kanban_get("T-999")
    assert set(missing) == {"ok", "error"}
    assert missing["ok"] is False
    assert "not found" in missing["error"]


def test_create_move_update_flow(store):
    created = mcp_server.kanban_create("MCP task")

    assert created["ok"] is True
    task = created["data"]
    assert task["id"] == "T-001"
    assert task["status"] == "backlog"
    assert task["project_id"] == "default"

    moved = mcp_server.kanban_move("T-001", "approved", comment="lgtm")
    assert moved["ok"] is True
    assert moved["data"]["status"] == "approved"

    pulled = mcp_server.kanban_pull("T-001")
    assert pulled["ok"] is True
    assert pulled["data"]["status"] == "analyst"

    updated = mcp_server.kanban_update("T-001", priority="high")
    assert updated["ok"] is True
    assert updated["data"]["priority"] == "high"

    full = mcp_server.kanban_get("T-001")
    actions = [h["action"] for h in full["data"]["history"]]
    assert actions == ["create", "move", "move", "update"]


def test_query_tools_contract(store):
    store.create_task("Searchable alpha", description="needle in description")
    store.create_task("Other")

    found = mcp_server.kanban_search("needle")
    assert found["ok"] is True
    assert found["data"]["count"] == 1
    assert found["data"]["tasks"][0]["title"] == "Searchable alpha"

    too_short = mcp_server.kanban_search("a")
    assert too_short["ok"] is False
    assert "at least 2 characters" in too_short["error"]

    board = mcp_server.kanban_board("default")
    assert board["ok"] is True
    assert board["data"]["total"] == 2
    assert set(board["data"]) == {"project", "total", "by_status"}

    unknown_board = mcp_server.kanban_board("nope")
    assert unknown_board["ok"] is False
    assert "not found" in unknown_board["error"]

    projects = mcp_server.kanban_projects()
    assert projects["ok"] is True
    assert {p["id"] for p in projects["data"]["projects"]} >= {"default"}


def test_link_and_blockers_contract(store):
    blocker = store.create_task("Blocker")
    target = store.create_task("Target")

    linked = mcp_server.kanban_link(target.id, "url", "https://example.com")
    assert linked == {
        "ok": True,
        "data": {"task_id": target.id, "link": {"type": "url", "value": "https://example.com"}},
    }

    bad_type = mcp_server.kanban_link(target.id, "psychic", "x")
    assert bad_type["ok"] is False

    blocked = mcp_server.kanban_blockers(target.id, [blocker.id])
    assert blocked == {
        "ok": True,
        "data": {"task_id": target.id, "blockers": [blocker.id]},
    }

    missing = mcp_server.kanban_blockers("T-999", [])
    assert missing["ok"] is False
