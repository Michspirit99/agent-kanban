"""Kanban — SQLite store.

Single source of truth: ``tasks.db`` (gitignored), or the path from env
``KANBAN_DB`` (see Store.__init__).

Public API:
    Store.list_tasks(status=..., assignee=...)
    Store.get_task(task_id)
    Store.create_task(title, status="backlog", ...)
    Store.move_task(task_id, to_status, actor, comment=None)
    Store.assign_task(task_id, assignee, actor)
    Store.add_comment(task_id, text, actor)
    Store.add_link(task_id, type, value)
    Store.set_blockers(task_id, blocker_ids)
    Store.snapshot()  -> dict (for JSON snapshots)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .migrations import BUSY_TIMEOUT_MS, apply_migrations
from .models import DEFAULT_PROJECT_ID, Issue, TaskHistory
from .snapshot_format import normalize_snapshot
from .snapshot_io import write_json_atomic
from .workflows import (
    WORKFLOW_ID_RE,
    Workflow,
    WorkflowError,
    WorkflowStatus,
    default_workflow,
    validate_workflow_statuses,
    workflow_settings,
)

# ``Task`` is the historical public name; ``Issue`` is the canonical model.
Task = Issue


class SnapshotImportConflict(ValueError):
    """Raised when imported durable data conflicts with an existing row."""

# ============================================================================
# Status model — derived from the central workflow registry (see
# kanban_store/workflows.py). ``STATUSES``/``status_meta`` describe the
# default workflow and remain as compatibility exports.
# ============================================================================

STATUSES: list[str] = default_workflow().status_keys()


def status_meta() -> list[dict[str, str]]:
    """Column metadata for the default workflow (id + label + owner)."""
    return default_workflow().columns()


# ============================================================================
# Models — canonical definitions live in kanban_store.models; ``Task`` is a
# compatibility alias for the canonical ``Issue`` model.
# ============================================================================


@dataclass
class Project:
    id: str
    name: str
    color: str
    icon: str
    sort_order: int
    archived: bool
    created_at: str
    path: str | None = None
    workflow_id: str = "default"
    task_counts: dict[str, int] = field(default_factory=dict)
    total_tasks: int = 0

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# Store
# ============================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_issue_fields(issue_type: str, labels: list[str] | None) -> None:
    if not isinstance(issue_type, str) or not issue_type.strip():
        raise ValueError("issue_type must be a non-empty string")
    if labels is not None:
        if not isinstance(labels, list) or not all(
            isinstance(label, str) for label in labels
        ):
            raise ValueError("labels must be a list of strings")


class Store:
    """Thread-safe wrapper around SQLite. One instance per process."""

    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = os.environ.get("KANBAN_DB") or (
                Path(__file__).resolve().parent.parent / "tasks.db"
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions use BEGIN
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Explicit (rather than relying on sqlite3.connect's default timeout)
        # so concurrent UI/MCP processes wait instead of failing fast.
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._migrate()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Delegate schema setup and upgrades to the migration runner."""
        with self._lock:
            apply_migrations(self._conn)

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='next_id'"
            ).fetchone()
            n = int(row["value"]) if row else 1
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_id'", (str(n + 1),)
            )
            return f"T-{n:03d}"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        status: str | Iterable[str] | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
    ) -> list[Task]:
        """List of tasks with filters, sorted by (status, column_order).

        ``project_id=None`` means "all projects". The board UI always
        passes a concrete project_id.
        """
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if assignee is not None:
            sql += " AND assignee = ?"
            params.append(assignee)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY status, column_order, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_task(r, eager_links=True) for r in rows]

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_task(row, eager_links=True, eager_history=True)

    def board(self) -> dict[str, list[Task]]:
        """Group tasks by status, in column order."""
        result: dict[str, list[Task]] = {s: [] for s in STATUSES}
        for t in self.list_tasks():
            result.setdefault(t.status, []).append(t)
        return result

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        status: str = "backlog",
        priority: str = "normal",
        size: str = "M",
        description: str = "",
        acceptance: str = "",
        assignee: str | None = None,
        external_blocker: str | None = None,
        actor: str = "user",
        links: list[dict[str, str]] | None = None,
        task_id: str | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
        issue_type: str = "task",
        reporter: str | None = None,
        labels: list[str] | None = None,
    ) -> Task:
        _validate_issue_fields(issue_type, labels)
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                project_row = self._conn.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if not project_row:
                    raise ValueError(f"project {project_id!r} not found")
                workflow = self.get_project_workflow(project_id)
                if status not in workflow.status_keys():
                    raise ValueError(
                        f"unknown status {status!r} for project workflow {workflow.id!r}"
                    )
                tid = task_id or self._next_id()
                # column_order — last in the column + 1 (per project)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status=? AND project_id=?",
                    (status, project_id),
                ).fetchone()
                col_order = (row["m"] + 1) if row else 0
                self._conn.execute(
                    """
                    INSERT INTO tasks (id, title, status, priority, size, assignee,
                                        description, acceptance, external_blocker,
                                        created_at, moved_at, column_order, project_id,
                                        issue_type, reporter, labels_json,
                                        custom_fields_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        title,
                        status,
                        priority,
                        size,
                        assignee,
                        description,
                        acceptance,
                        external_blocker,
                        ts,
                        ts,
                        col_order,
                        project_id,
                        issue_type,
                        reporter,
                        json.dumps(labels or [], ensure_ascii=False),
                        "{}",
                        ts,
                    ),
                )
                if links:
                    for ln in links:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                            (tid, ln["type"], ln["value"]),
                        )
                self._conn.execute(
                    """
                    INSERT INTO task_history (task_id, ts, actor, action, from_status, to_status, comment)
                    VALUES (?, ?, ?, 'create', NULL, ?, ?)
                    """,
                    (tid, ts, actor, status, None),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(tid)
        assert task is not None
        return task

    def move_task(
        self,
        task_id: str,
        to_status: str,
        *,
        actor: str = "user",
        comment: str | None = None,
        column_order: int | None = None,
    ) -> Task:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                from_status = row["status"]
                project_id = row["project_id"]
                workflow = self.get_project_workflow(project_id)
                if to_status not in workflow.status_keys():
                    raise ValueError(
                        f"unknown status {to_status!r} for project workflow {workflow.id!r}"
                    )
                # column_order — append to the end of the project's column when not specified
                if column_order is None:
                    r2 = self._conn.execute(
                        "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                        "WHERE status=? AND project_id=?",
                        (to_status, project_id),
                    ).fetchone()
                    column_order = (r2["m"] + 1) if r2 else 0
                self._conn.execute(
                    """UPDATE tasks SET status=?, moved_at=?, column_order=?
                       WHERE id=?""",
                    (to_status, ts, column_order, task_id),
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'move', ?, ?, ?)""",
                    (task_id, ts, actor, from_status, to_status, comment),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(task_id)
        assert task is not None
        return task

    def assign_task(self, task_id: str, assignee: str | None, *, actor: str) -> Task:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT assignee FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                self._conn.execute(
                    "UPDATE tasks SET assignee=? WHERE id=?", (assignee, task_id)
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'assign', NULL, NULL, ?)""",
                    (task_id, ts, actor, f"assignee → {assignee}"),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def pull_task(self, task_id: str, assignee: str = "claude") -> Task:
        """Atomic claim: assignee IS NULL → assignee, claim_from → claim_to.

        The claim transition comes from the task's project workflow settings
        (default workflow: approved → analyst). Used by agents for a safe
        "claim the task" operation.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT assignee, status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                workflow = self.get_project_workflow(row["project_id"])
                settings = workflow_settings(workflow)
                claim_from = settings["claim_from"]
                claim_to = settings["claim_to"]
                workflow_keys = set(workflow.status_keys())
                if claim_from not in workflow_keys or claim_to not in workflow_keys:
                    raise ValueError(
                        f"workflow {workflow.id!r} settings reference unknown statuses"
                    )
                if row["assignee"] is not None and row["assignee"] != assignee:
                    raise RuntimeError(
                        f"task {task_id} already assigned to {row['assignee']}"
                    )
                if row["status"] != claim_from:
                    raise RuntimeError(
                        f"task {task_id} is in '{row['status']}', not '{claim_from}'"
                    )
                # move to claim_to and claim (per project)
                r2 = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status=? AND project_id=?",
                    (claim_to, row["project_id"]),
                ).fetchone()
                col_order = (r2["m"] + 1) if r2 else 0
                self._conn.execute(
                    """UPDATE tasks SET status=?, assignee=?, moved_at=?, column_order=?
                       WHERE id=?""",
                    (claim_to, assignee, ts, col_order, task_id),
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'move', ?, ?, 'pulled')""",
                    (task_id, ts, assignee, claim_from, claim_to),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def add_comment(self, task_id: str, text: str, *, actor: str) -> None:
        ts = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            self._conn.execute(
                """INSERT INTO task_history
                   (task_id, ts, actor, action, from_status, to_status, comment)
                   VALUES (?, ?, ?, 'comment', NULL, NULL, ?)""",
                (task_id, ts, actor, text),
            )

    def add_link(self, task_id: str, type_: str, value: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            self._conn.execute(
                "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                (task_id, type_, value),
            )

    def set_blockers(self, task_id: str, blocker_ids: list[str]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                for blocker_id in blocker_ids:
                    if blocker_id == task_id:
                        raise ValueError("a task cannot block itself")
                    blocker = self._conn.execute(
                        "SELECT 1 FROM tasks WHERE id=?", (blocker_id,)
                    ).fetchone()
                    if not blocker:
                        raise ValueError(f"blocker task {blocker_id} not found")
                self._conn.execute(
                    "DELETE FROM task_blockers WHERE task_id=?", (task_id,)
                )
                for blocker_id in blocker_ids:
                    self._conn.execute(
                        "INSERT INTO task_blockers (task_id, blocker_id) VALUES (?, ?)",
                        (task_id, blocker_id),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_fields(
        self,
        task_id: str,
        *,
        actor: str,
        title: str | None = None,
        priority: str | None = None,
        size: str | None = None,
        description: str | None = None,
        acceptance: str | None = None,
        external_blocker: str | None = None,
        issue_type: str | None = None,
        reporter: str | None = None,
        labels: list[str] | None = None,
    ) -> Task:
        if issue_type is not None:
            _validate_issue_fields(issue_type, None)
        if labels is not None and (
            not isinstance(labels, list)
            or not all(isinstance(label, str) for label in labels)
        ):
            raise ValueError("labels must be a list of strings")
        ts = _now()
        sets: list[str] = []
        params: list[Any] = []
        changed: list[str] = []
        for col, val in (
            ("title", title),
            ("priority", priority),
            ("size", size),
            ("description", description),
            ("acceptance", acceptance),
            ("external_blocker", external_blocker),
            ("issue_type", issue_type),
            ("reporter", reporter),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
                changed.append(col)
        if labels is not None:
            sets.append("labels_json = ?")
            params.append(json.dumps(labels, ensure_ascii=False))
            changed.append("labels")
        if not sets:
            t = self.get_task(task_id)
            if t is None:
                raise KeyError(task_id)
            return t
        sets.append("updated_at = ?")
        params.append(ts)
        params.append(task_id)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                self._conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
                )
                self._conn.execute(
                    """INSERT INTO task_history
                       (task_id, ts, actor, action, from_status, to_status, comment)
                       VALUES (?, ?, ?, 'update', NULL, NULL, ?)""",
                    (task_id, ts, actor, ", ".join(changed)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id)
        assert t is not None
        return t

    def reorder(self, task_id: str, new_order: int) -> None:
        """Change the order within the current column."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET column_order=? WHERE id=?", (new_order, task_id)
            )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """All projects with task_counts aggregated by status."""
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY sort_order, name"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            counts_rows = self._conn.execute(
                "SELECT project_id, status, COUNT(*) AS n "
                "FROM tasks GROUP BY project_id, status"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for r in counts_rows:
            counts.setdefault(r["project_id"], {})[r["status"]] = r["n"]
            totals[r["project_id"]] = totals.get(r["project_id"], 0) + r["n"]
        result = []
        for r in rows:
            p = self._row_to_project(r)
            p.task_counts = counts.get(p.id, {})
            p.total_tasks = totals.get(p.id, 0)
            result.append(p)
        return result

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def create_project(
        self,
        project_id: str,
        name: str,
        *,
        color: str = "#F10D30",
        icon: str = "",
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if sort_order is None:
                    r = self._conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM projects"
                    ).fetchone()
                    sort_order = (r["m"] + 1) if r else 0
                self._conn.execute(
                    """INSERT INTO projects
                       (id, name, color, icon, sort_order, archived, path, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (project_id, name, color, icon or name[:1].upper(), sort_order, path, ts),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        p = self.get_project(project_id)
        assert p is not None
        return p

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        sort_order: int | None = None,
        path: str | None = None,
    ) -> Project:
        sets: list[str] = []
        params: list[Any] = []
        # path is forwarded as-is (None means "leave alone", "" means "clear").
        for col, val in (
            ("name", name), ("color", color), ("icon", icon),
            ("sort_order", sort_order), ("path", path),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                # an empty string for path becomes NULL in the database
                params.append(None if (col == "path" and val == "") else val)
        if not sets:
            p = self.get_project(project_id)
            if p is None:
                raise KeyError(project_id)
            return p
        params.append(project_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    # ------------------------------------------------------------------
    # Workflows (central registry; see kanban_store/workflows.py)
    # ------------------------------------------------------------------

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._lock:
            workflow_row = self._conn.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if not workflow_row:
                return None
            status_rows = self._conn.execute(
                "SELECT * FROM workflow_statuses WHERE workflow_id=? "
                "ORDER BY position, key",
                (workflow_id,),
            ).fetchall()
        return self._workflow_from_rows(workflow_row, status_rows)

    def list_workflows(self) -> list[Workflow]:
        with self._lock:
            workflow_rows = self._conn.execute(
                "SELECT * FROM workflows ORDER BY id"
            ).fetchall()
            status_rows = self._conn.execute(
                "SELECT * FROM workflow_statuses ORDER BY workflow_id, position, key"
            ).fetchall()
        by_workflow: dict[str, list[sqlite3.Row]] = {}
        for row in status_rows:
            by_workflow.setdefault(row["workflow_id"], []).append(row)
        return [
            self._workflow_from_rows(workflow_row, by_workflow.get(workflow_row["id"], []))
            for workflow_row in workflow_rows
        ]

    def get_project_workflow(self, project_id: str) -> Workflow:
        """The project's workflow, or the built-in default as fallback."""
        with self._lock:
            row = self._conn.execute(
                "SELECT workflow_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        workflow_id = row["workflow_id"] if row else None
        workflow = self.get_workflow(workflow_id) if workflow_id else None
        return workflow if workflow is not None else default_workflow()

    def create_workflow(
        self,
        workflow_id: str,
        name: str,
        statuses: list[dict[str, Any]],
    ) -> Workflow:
        if not isinstance(workflow_id, str) or not WORKFLOW_ID_RE.fullmatch(workflow_id):
            raise WorkflowError("workflow id must match ^[a-z][a-z0-9-]{0,31}$")
        if not isinstance(name, str) or not name.strip():
            raise WorkflowError("workflow name must be a non-empty string")
        validated = validate_workflow_statuses(statuses)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                exists = self._conn.execute(
                    "SELECT 1 FROM workflows WHERE id=?", (workflow_id,)
                ).fetchone()
                if exists:
                    raise ValueError(f"workflow {workflow_id!r} already exists")
                self._conn.execute(
                    "INSERT INTO workflows (id, name, settings_json, created_at) "
                    "VALUES (?, ?, '{}', ?)",
                    (workflow_id, name, _now()),
                )
                for status in validated:
                    self._conn.execute(
                        "INSERT INTO workflow_statuses "
                        "(workflow_id, key, label, owner, position, active) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (
                            workflow_id, status.key, status.label,
                            status.owner, status.position,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        created = self.get_workflow(workflow_id)
        assert created is not None
        return created

    def set_project_workflow(self, project_id: str, workflow_id: str) -> None:
        """Assign a workflow to a project.

        Rejected when existing tasks in the project use statuses that the
        target workflow does not define — data is never rewritten.
        """
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"workflow {workflow_id!r} not found")
        allowed = set(workflow.status_keys())
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                project_row = self._conn.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if not project_row:
                    raise KeyError(project_id)
                rows = self._conn.execute(
                    "SELECT DISTINCT status FROM tasks WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                unmappable = sorted(
                    row["status"] for row in rows if row["status"] not in allowed
                )
                if unmappable:
                    raise ValueError(
                        f"cannot assign workflow {workflow_id!r}: tasks use "
                        f"statuses not in it: {unmappable}"
                    )
                self._conn.execute(
                    "UPDATE projects SET workflow_id=? WHERE id=?",
                    (workflow_id, project_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _workflow_from_rows(
        workflow_row: sqlite3.Row, status_rows: list[sqlite3.Row]
    ) -> Workflow:
        return Workflow(
            id=workflow_row["id"],
            name=workflow_row["name"],
            statuses=tuple(
                WorkflowStatus(
                    key=row["key"],
                    label=row["label"],
                    owner=row["owner"],
                    position=row["position"],
                    active=bool(row["active"]),
                )
                for row in status_rows
            ),
            settings=(
                json.loads(workflow_row["settings_json"])
                if workflow_row["settings_json"]
                else {}
            ),
        )

    # ------------------------------------------------------------------
    # Project sources (one source per project)
    # ------------------------------------------------------------------

    def set_project_source(
        self, project_id: str, type_: str, config: dict[str, Any]
    ) -> None:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO project_sources (project_id, type, config, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE
                   SET type=excluded.type, config=excluded.config""",
                (project_id, type_, json.dumps(config, ensure_ascii=False), ts),
            )

    def get_project_source(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_sources WHERE project_id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "project_id": row["project_id"],
            "type": row["type"],
            "config": json.loads(row["config"]),
            "last_sync_at": row["last_sync_at"],
            "created_at": row["created_at"],
        }

    def update_source_sync_time(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE project_sources SET last_sync_at=? WHERE project_id=?",
                (_now(), project_id),
            )

    def delete_project_source(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_sources WHERE project_id=?", (project_id,)
            )

    def archive_project(self, project_id: str, archived: bool = True) -> Project:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                "UPDATE projects SET archived=? WHERE id=?",
                (1 if archived else 0, project_id),
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            icon=row["icon"],
            sort_order=row["sort_order"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            path=row["path"] if "path" in row.keys() else None,
            workflow_id=(
                row["workflow_id"] if "workflow_id" in row.keys() else "default"
            ),
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Dump the whole board as a plain dict for JSON persistence.

        The explicit read transaction keeps the project, task, relation, and
        history queries on one SQLite snapshot.  Keeping all queries under the
        Store lock also prevents this Store's writers from interleaving with
        the export.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                schema_row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                database_schema_version = int(schema_row["value"]) if schema_row else 0

                project_rows = self._conn.execute(
                    "SELECT * FROM projects ORDER BY sort_order, name"
                ).fetchall()
                count_rows = self._conn.execute(
                    "SELECT project_id, status, COUNT(*) AS n "
                    "FROM tasks GROUP BY project_id, status"
                ).fetchall()
                task_rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY status, column_order, id"
                ).fetchall()
                link_rows = self._conn.execute(
                    "SELECT task_id, type, value FROM task_links "
                    "ORDER BY task_id, type, value"
                ).fetchall()
                blocker_rows = self._conn.execute(
                    "SELECT task_id, blocker_id FROM task_blockers "
                    "ORDER BY task_id, blocker_id"
                ).fetchall()
                history_rows = self._conn.execute(
                    "SELECT * FROM task_history ORDER BY task_id, ts ASC, id ASC"
                ).fetchall()

                counts: dict[str, dict[str, int]] = {}
                totals: dict[str, int] = {}
                for row in count_rows:
                    counts.setdefault(row["project_id"], {})[row["status"]] = row["n"]
                    totals[row["project_id"]] = totals.get(row["project_id"], 0) + row["n"]

                links: dict[str, list[dict[str, str]]] = {}
                for row in link_rows:
                    links.setdefault(row["task_id"], []).append(
                        {"type": row["type"], "value": row["value"]}
                    )
                blockers: dict[str, list[str]] = {}
                for row in blocker_rows:
                    blockers.setdefault(row["task_id"], []).append(row["blocker_id"])
                histories: dict[str, list[dict[str, Any]]] = {}
                for row in history_rows:
                    histories.setdefault(row["task_id"], []).append(
                        {
                            "id": row["id"],
                            "task_id": row["task_id"],
                            "ts": row["ts"],
                            "actor": row["actor"],
                            "action": row["action"],
                            "from_status": row["from_status"],
                            "to_status": row["to_status"],
                            "comment": row["comment"],
                        }
                    )

                projects: list[dict[str, Any]] = []
                for row in project_rows:
                    project = self._row_to_project(row)
                    project.task_counts = counts.get(project.id, {})
                    project.total_tasks = totals.get(project.id, 0)
                    projects.append(project.to_public())

                tasks: list[dict[str, Any]] = []
                for row in task_rows:
                    task = self._row_to_task(row)
                    task.links = links.get(task.id, [])
                    task.blockers = blockers.get(task.id, [])
                    task.history = [
                        TaskHistory(
                            id=entry["id"],
                            task_id=entry["task_id"],
                            ts=entry["ts"],
                            actor=entry["actor"],
                            action=entry["action"],
                            from_status=entry["from_status"],
                            to_status=entry["to_status"],
                            comment=entry["comment"],
                        )
                        for entry in histories.get(task.id, [])
                    ]
                    tasks.append(task.to_public())

                result = {
                    "exported_at": _now(),
                    "schema_version": 2,
                    "snapshot_version": 1,
                    "database_schema_version": database_schema_version,
                    "projects": projects,
                    "tasks": tasks,
                }
                self._conn.execute("COMMIT")
                return result
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def save_snapshot(self, dest_dir: str | Path | None = None) -> Path:
        if dest_dir is None:
            dest_dir = Path(__file__).resolve().parent.parent / "snapshots"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fp = dest_dir / f"{date_part}.json"
        return write_json_atomic(fp, self.snapshot())

    def import_snapshot(self, payload: Any) -> dict[str, int]:
        """Add the durable rows in a normalized snapshot to this Store.

        Existing rows are left untouched.  A row with the same durable
        identity and different values is a conflict; all such checks happen
        before any insert in the transaction.
        """
        snapshot = normalize_snapshot(payload)
        self._validate_import_relations(snapshot)

        projects = snapshot["projects"]
        tasks = snapshot["tasks"]
        history = [entry for task in tasks for entry in task["history"]]
        max_imported_id = max(
            (
                int(match.group(1))
                for task in tasks
                if (match := re.fullmatch(r"T-(\d+)", task["id"]))
            ),
            default=0,
        )

        inserted = {"projects": 0, "tasks": 0, "links": 0, "blockers": 0, "history": 0}
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                existing_projects = {
                    row["id"]: row
                    for row in self._conn.execute("SELECT * FROM projects").fetchall()
                }
                existing_tasks = {
                    row["id"]: row
                    for row in self._conn.execute("SELECT * FROM tasks").fetchall()
                }
                existing_history = {
                    row["id"]: row
                    for row in self._conn.execute("SELECT * FROM task_history").fetchall()
                }

                for project in projects:
                    current = existing_projects.get(project["id"])
                    if current is not None:
                        self._check_project_conflict(current, project)
                task_columns = (
                    "id", "title", "status", "priority", "size", "assignee",
                    "description", "acceptance", "external_blocker", "created_at",
                    "moved_at", "column_order", "project_id",
                )
                for task in tasks:
                    current = existing_tasks.get(task["id"])
                    if current is not None:
                        if any(
                            current[column] != task[column] for column in task_columns
                        ):
                            raise SnapshotImportConflict(
                                f"task {task['id']!r} conflicts with existing durable data"
                            )
                        if (
                            current["issue_type"] != task.get("issue_type", "task")
                            or current["reporter"] != task.get("reporter")
                            or current["updated_at"]
                            != (task.get("updated_at") or task["created_at"])
                            or json.loads(current["labels_json"] or "[]")
                            != task.get("labels", [])
                        ):
                            raise SnapshotImportConflict(
                                f"task {task['id']!r} conflicts with existing durable data"
                            )
                history_columns = (
                    "id", "task_id", "ts", "actor", "action",
                    "from_status", "to_status", "comment",
                )
                for entry in history:
                    current = existing_history.get(entry["id"])
                    if current is not None and any(
                        current[column] != entry[column] for column in history_columns
                    ):
                        raise SnapshotImportConflict(
                            f"history {entry['id']!r} conflicts with existing durable data"
                        )

                for project in projects:
                    if project["id"] in existing_projects:
                        continue
                    self._conn.execute(
                        """INSERT INTO projects
                           (id, name, color, icon, sort_order, archived, path,
                            workflow_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            project["id"], project["name"], project["color"],
                            project["icon"], project["sort_order"],
                            int(project["archived"]), project.get("path"),
                            project.get("workflow_id", "default"),
                            project["created_at"],
                        ),
                    )
                    inserted["projects"] += 1

                for task in tasks:
                    if task["id"] in existing_tasks:
                        continue
                    self._conn.execute(
                        """INSERT INTO tasks
                           (id, title, status, priority, size, assignee,
                            description, acceptance, external_blocker,
                            created_at, moved_at, column_order, project_id,
                            issue_type, reporter, labels_json,
                            custom_fields_json, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            task["id"], task["title"], task["status"],
                            task["priority"], task["size"], task["assignee"],
                            task["description"], task["acceptance"],
                            task["external_blocker"], task["created_at"],
                            task["moved_at"], task["column_order"],
                            task["project_id"],
                            task.get("issue_type", "task"),
                            task.get("reporter"),
                            json.dumps(task.get("labels", []), ensure_ascii=False),
                            json.dumps(task.get("custom_fields", {}), ensure_ascii=False),
                            task.get("updated_at") or task["created_at"],
                        ),
                    )
                    inserted["tasks"] += 1

                for task in tasks:
                    for link in task["links"]:
                        cursor = self._conn.execute(
                            "INSERT OR IGNORE INTO task_links (task_id, type, value) "
                            "VALUES (?, ?, ?)",
                            (task["id"], link["type"], link["value"]),
                        )
                        inserted["links"] += cursor.rowcount
                    for blocker_id in task["blockers"]:
                        cursor = self._conn.execute(
                            "INSERT OR IGNORE INTO task_blockers (task_id, blocker_id) "
                            "VALUES (?, ?)", (task["id"], blocker_id)
                        )
                        inserted["blockers"] += cursor.rowcount
                    for entry in task["history"]:
                        if entry["id"] in existing_history:
                            continue
                        self._conn.execute(
                            """INSERT INTO task_history
                               (id, task_id, ts, actor, action, from_status,
                                to_status, comment)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            tuple(entry[column] for column in history_columns),
                        )
                        inserted["history"] += 1

                next_id_row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='next_id'"
                ).fetchone()
                current_next_id = int(next_id_row["value"]) if next_id_row else 1
                next_id = max(current_next_id, max_imported_id + 1)
                if next_id_row and next_id != current_next_id:
                    self._conn.execute(
                        "UPDATE meta SET value=? WHERE key='next_id'", (str(next_id),)
                    )
                elif not next_id_row:
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('next_id', ?)",
                        (str(next_id),),
                    )
                self._conn.execute("COMMIT")
                return inserted
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_import_relations(snapshot: dict[str, Any]) -> None:
        """Validate Store-specific constraints before opening writes."""
        history_ids: set[int] = set()
        link_ids: set[tuple[str, str, str]] = set()
        for task in snapshot["tasks"]:
            if task["status"] not in STATUSES:
                raise ValueError(f"unknown task status {task['status']!r}")
            for link in task["links"]:
                identity = (task["id"], link["type"], link["value"])
                if identity in link_ids:
                    raise ValueError(f"duplicate link {identity!r} in snapshot")
                link_ids.add(identity)
            for entry in task["history"]:
                if entry["id"] in history_ids:
                    raise ValueError(
                        f"duplicate history id {entry['id']!r} in snapshot"
                    )
                history_ids.add(entry["id"])

    @staticmethod
    def _check_project_conflict(current: sqlite3.Row, project: dict[str, Any]) -> None:
        # Fresh Store instances bootstrap the default project with a new
        # created_at timestamp.  Treat that timestamp as non-conflicting so a
        # normal snapshot can be imported into a fresh database.
        columns = ("id", "name", "color", "icon", "sort_order", "archived")
        if any(
            (bool(current[column]) if column == "archived" else current[column])
            != project[column]
            for column in columns
        ):
            raise SnapshotImportConflict(
                f"project {project['id']!r} conflicts with existing durable data"
            )
        if "path" in project and current["path"] != project["path"]:
            raise SnapshotImportConflict(
                f"project {project['id']!r} conflicts with existing durable data"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_task(
        self,
        row: sqlite3.Row,
        *,
        eager_links: bool = False,
        eager_history: bool = False,
    ) -> Task:
        t = Task(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            size=row["size"],
            assignee=row["assignee"],
            description=row["description"],
            acceptance=row["acceptance"],
            external_blocker=row["external_blocker"],
            created_at=row["created_at"],
            moved_at=row["moved_at"],
            column_order=row["column_order"],
            project_id=row["project_id"] if "project_id" in row.keys() else DEFAULT_PROJECT_ID,
            issue_type=(
                row["issue_type"] if "issue_type" in row.keys() else "task"
            ),
            reporter=row["reporter"] if "reporter" in row.keys() else None,
            labels=(
                json.loads(row["labels_json"])
                if "labels_json" in row.keys() and row["labels_json"]
                else []
            ),
            custom_fields=(
                json.loads(row["custom_fields_json"])
                if "custom_fields_json" in row.keys() and row["custom_fields_json"]
                else {}
            ),
            updated_at=(
                (row["updated_at"] or row["created_at"])
                if "updated_at" in row.keys()
                else row["created_at"]
            ),
        )
        if eager_links:
            link_rows = self._conn.execute(
                "SELECT type, value FROM task_links WHERE task_id=? ORDER BY type, value",
                (t.id,),
            ).fetchall()
            t.links = [{"type": r["type"], "value": r["value"]} for r in link_rows]
            blocker_rows = self._conn.execute(
                "SELECT blocker_id FROM task_blockers WHERE task_id=?", (t.id,)
            ).fetchall()
            t.blockers = [r["blocker_id"] for r in blocker_rows]
        if eager_history:
            h_rows = self._conn.execute(
                "SELECT * FROM task_history WHERE task_id=? ORDER BY ts ASC, id ASC",
                (t.id,),
            ).fetchall()
            t.history = [
                TaskHistory(
                    id=r["id"],
                    task_id=r["task_id"],
                    ts=r["ts"],
                    actor=r["actor"],
                    action=r["action"],
                    from_status=r["from_status"],
                    to_status=r["to_status"],
                    comment=r["comment"],
                )
                for r in h_rows
            ]
        return t

    def close(self) -> None:
        with self._lock:
            self._conn.close()
