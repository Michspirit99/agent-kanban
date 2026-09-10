-- Kanban schema (SQLite)
-- v2: added projects + tasks.project_id (migration in Store._migrate_v2).

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,                    -- 'finops', 'kanban-dev', ...
    name        TEXT NOT NULL,                       -- 'FinOps', 'Kanban Dev'
    color       TEXT NOT NULL DEFAULT '#F10D30',     -- accent color of the project (hex)
    icon        TEXT NOT NULL DEFAULT '',            -- 1-2 chars: 'F', 'KB', 'AI'
    sort_order  INTEGER NOT NULL DEFAULT 0,          -- order in the project switcher
    archived    INTEGER NOT NULL DEFAULT 0,          -- 0/1
    path        TEXT,                                -- Claude Code project directory (optional)
    workflow_id TEXT NOT NULL DEFAULT 'default',     -- workflow registry id
    created_at  TEXT NOT NULL                        -- ISO8601
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,                -- T-001, T-002, ...
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'backlog', -- backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled
    priority        TEXT NOT NULL DEFAULT 'normal',  -- high/normal/low
    size            TEXT NOT NULL DEFAULT 'M',       -- S/M/L
    assignee        TEXT,                            -- user / agent:<name> / NULL
    description     TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '',
    external_blocker TEXT,                           -- "DevOps: roles monitoring.viewer"
    created_at      TEXT NOT NULL,                   -- ISO8601
    moved_at        TEXT NOT NULL,                   -- ISO8601, last status change
    column_order    INTEGER NOT NULL DEFAULT 0,      -- order within the column (for drag-drop)
    project_id      TEXT NOT NULL DEFAULT 'default', -- FK -> projects.id
    issue_type      TEXT NOT NULL DEFAULT 'task',    -- task/bug/story/epic (free-form key)
    reporter        TEXT,                            -- who reported the issue
    labels_json     TEXT NOT NULL DEFAULT '[]',      -- JSON array of label strings
    custom_fields_json TEXT NOT NULL DEFAULT '{}',   -- JSON object of custom field values
    updated_at      TEXT                             -- ISO8601, last content edit (not moves)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, column_order);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
-- idx_tasks_project_status is created in Store._migrate_v2 (after ALTER TABLE for older databases).

CREATE TABLE IF NOT EXISTS task_links (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type     TEXT NOT NULL,                          -- memory/file/pr/url
    value    TEXT NOT NULL,
    PRIMARY KEY (task_id, type, value)
);

CREATE TABLE IF NOT EXISTS task_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,                      -- user/agent:<name>
    action       TEXT NOT NULL,                      -- create/move/comment/assign
    from_status  TEXT,
    to_status    TEXT,
    comment      TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_task ON task_history(task_id, ts);

CREATE TABLE IF NOT EXISTS task_blockers (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, blocker_id),
    CHECK (task_id != blocker_id)
);

CREATE TABLE IF NOT EXISTS project_sources (
    project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,              -- 'plan_md' | 'git'
    config        TEXT NOT NULL,              -- JSON: {file, repo_url, ...}
    last_sync_at  TEXT,                       -- ISO8601
    created_at    TEXT NOT NULL
);

-- Central workflow registry (v6). The default workflow mirrors the original
-- nine-column board; projects may override it via projects.workflow_id.
CREATE TABLE IF NOT EXISTS workflows (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}', -- reserved: claim/active-status config
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_statuses (
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    label       TEXT NOT NULL,
    owner       TEXT NOT NULL DEFAULT 'any',  -- user | agent | any
    position    INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (workflow_id, key)
);

INSERT OR IGNORE INTO workflows (id, name, settings_json, created_at)
    VALUES ('default', 'Default workflow',
            '{"claim_from": "approved", "claim_to": "analyst", "active_statuses": ["analyst", "in_progress", "testing"]}',
            '2026-01-01T00:00:00+00:00');

INSERT OR IGNORE INTO workflow_statuses (workflow_id, key, label, owner, position, active) VALUES
    ('default', 'backlog',     'Backlog',     'user',  0, 1),
    ('default', 'approved',    'Approved',    'agent', 1, 1),
    ('default', 'analyst',     'Analyst',     'agent', 2, 1),
    ('default', 'in_progress', 'In progress', 'agent', 3, 1),
    ('default', 'testing',     'Testing',     'agent', 4, 1),
    ('default', 'uat',         'UAT',         'user',  5, 1),
    ('default', 'done',        'Done',        'user',  6, 1),
    ('default', 'blocked',     'Blocked',     'any',   7, 1),
    ('default', 'cancelled',   'Cancelled',   'user',  8, 1);

CREATE TABLE IF NOT EXISTS issue_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    task_id      TEXT,
    project_id   TEXT,
    actor        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_issue_events_pending
    ON issue_events(delivered_at, id);

-- Full-text search index (v8): external-content FTS5 table mirrored from
-- tasks via triggers, plus sync triggers. The table is created by migration
-- _migrate_v8 (which also backfills it), not here — so databases on SQLite
-- builds without FTS5 can still open (migration records 'fts5'='off').

-- meta for migrations
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '8');
INSERT OR IGNORE INTO meta(key, value) VALUES ('next_id', '1');

-- Default project — read from env ``KANBAN_DEFAULT_PROJECT_ID`` / ``..._NAME``
-- (see Store._seed_default_project). If not provided, 'default'/'Default' is created.
-- Existing tasks receive this project_id via _migrate_v2().
