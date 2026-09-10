"""Ordered, transactional, idempotent SQLite schema migrations.

The runner applies ``schema.sql`` as an idempotent baseline and then executes
each registered migration whose target version is newer than the database's
recorded ``meta.schema_version``. Version bumps happen only after a migration
succeeds, so a failure leaves the previous schema and data untouched.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("kanban.store.migrations")

SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"
LATEST_VERSION = 6
BUSY_TIMEOUT_MS = 5000

MigrationFn = Callable[[sqlite3.Connection], None]

# Registry entries: (target_version, name, fn, always_run).
# ``always_run`` marks idempotent migrations that must also execute on fresh
# databases (where the recorded version is already the latest) because they
# bootstrap baseline data as well as schema.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: adds tasks.project_id and seeds the default project.

    Idempotent; also runs on fresh databases to create the project/status
    index and bootstrap the default project (matching historical behavior).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "project_id" not in cols:
        default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
        conn.execute(
            f"ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default_id}'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_project_status "
        "ON tasks(project_id, status, column_order)"
    )
    row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
    if row[0] == 0:
        default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
        default_name = os.environ.get("KANBAN_DEFAULT_PROJECT_NAME", "Default")
        default_color = os.environ.get("KANBAN_DEFAULT_PROJECT_COLOR", "#F10D30")
        default_icon = os.environ.get(
            "KANBAN_DEFAULT_PROJECT_ICON", default_name[:1].upper()
        )
        conn.execute(
            "INSERT INTO projects (id, name, color, icon, sort_order, archived, created_at) "
            "VALUES (?, ?, ?, ?, 0, 0, ?)",
            (default_id, default_name, default_color, default_icon, _now()),
        )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: projects.path TEXT (Claude Code project directory)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "path" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN path TEXT")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: project_sources table (created via schema.sql baseline)."""


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5: additive issue fields with safe defaults.

    Adds issue_type, reporter, labels_json, custom_fields_json and
    updated_at to ``tasks`` without renaming or rewriting existing data;
    backfills ``updated_at`` from ``created_at`` so legacy rows keep a
    meaningful last-edit timestamp.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "issue_type" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN issue_type TEXT NOT NULL DEFAULT 'task'"
        )
    if "reporter" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN reporter TEXT")
    if "labels_json" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "custom_fields_json" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN custom_fields_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6: projects.workflow_id for the central workflow registry.

    Workflow tables and the default-workflow seed are created by the
    schema.sql baseline (idempotent ``IF NOT EXISTS`` / ``INSERT OR IGNORE``);
    this migration only adds the project assignment column for existing
    databases. All projects default to the 'default' workflow.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "workflow_id" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN workflow_id TEXT NOT NULL DEFAULT 'default'"
        )
    conn.execute(
        "UPDATE projects SET workflow_id='default' WHERE workflow_id IS NULL"
    )


MIGRATIONS: list[tuple[int, str, MigrationFn, bool]] = [
    (2, "tasks.project_id + default project bootstrap", _migrate_v2, True),
    (3, "projects.path", _migrate_v3, False),
    (4, "project_sources", _migrate_v4, False),
    (
        5,
        "issue fields (issue_type/reporter/labels/custom_fields/updated_at)",
        _migrate_v5,
        False,
    ),
    (6, "workflows + projects.workflow_id", _migrate_v6, False),
]


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 1


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    schema_sql_path: Path = SCHEMA_SQL_PATH,
    migrations: list[tuple[int, str, MigrationFn, bool]] | None = None,
) -> int:
    """Bring *conn* to the latest schema version; returns the version.

    ``schema.sql`` is applied first as an idempotent baseline. Each pending
    migration runs inside one ``BEGIN IMMEDIATE`` transaction; the recorded
    version advances only after the migration body succeeds, so a failure
    rolls back and leaves the database at its previous version.
    """
    conn.executescript(Path(schema_sql_path).read_text(encoding="utf-8"))
    version = _read_version(conn)
    for target, name, fn, always_run in (
        migrations if migrations is not None else MIGRATIONS
    ):
        if target <= version and not always_run:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            if target > version:
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(target),),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if target > version:
            version = target
        log.info("migration applied: v%s %s", target, name)
    _run_integrity_checks(conn)
    return version


def _run_integrity_checks(conn: sqlite3.Connection) -> None:
    """Report (non-fatal) foreign-key violations; fail on real corruption.

    Foreign-key violations are logged rather than raised because legacy
    databases may legitimately contain orphan ``tasks.project_id`` values
    (the column predates enforcement) and startup must not brick them.
    """
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        log.warning(
            "foreign_key_check reported %d violation(s); first rows: %s",
            len(violations),
            [tuple(v) for v in violations[:5]],
        )
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result and result[0] != "ok":
        raise RuntimeError(f"database integrity_check failed: {result[0]}")


__all__ = [
    "BUSY_TIMEOUT_MS",
    "LATEST_VERSION",
    "MIGRATIONS",
    "SCHEMA_SQL_PATH",
    "apply_migrations",
]
