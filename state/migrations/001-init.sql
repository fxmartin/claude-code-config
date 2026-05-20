-- state/migrations/001-init.sql
--
-- Bootstrap migration for the SDLC ledger (Story 4.1-001). Establishes the
-- initial set of tables consumed by Stories 4.2-001, 4.2-002, and 4.3-001.
--
-- Every statement here is idempotent (`CREATE TABLE IF NOT EXISTS`) so that
-- the migration runner can re-attempt this migration safely if a previous
-- run was interrupted mid-way; the `_migrations` bookkeeping is what makes
-- the runner skip an already-applied migration on the happy path.

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    scope           TEXT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    mode            TEXT,
    total_stories   INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'IN_PROGRESS'
);

CREATE TABLE IF NOT EXISTS stories (
    run_id          TEXT NOT NULL,
    story_id        TEXT NOT NULL,
    epic_id         TEXT,
    title           TEXT,
    priority        TEXT,
    points          INTEGER,
    agent_type      TEXT,
    branch          TEXT,
    pr_number       INTEGER,
    current_stage   TEXT,
    status          TEXT NOT NULL DEFAULT 'TODO',
    PRIMARY KEY (run_id, story_id),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stages (
    run_id              TEXT NOT NULL,
    story_id            TEXT NOT NULL,
    stage_name          TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    failure_category    TEXT,
    output_path         TEXT,
    PRIMARY KEY (run_id, story_id, stage_name, attempt),
    FOREIGN KEY (run_id, story_id) REFERENCES stories(run_id, story_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    story_id    TEXT,
    ts          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level       TEXT NOT NULL,
    source      TEXT,
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stories_status      ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stages_status       ON stages(status);
CREATE INDEX IF NOT EXISTS idx_runs_status         ON runs(status);
CREATE INDEX IF NOT EXISTS idx_events_run_ts       ON events(run_id, ts);
