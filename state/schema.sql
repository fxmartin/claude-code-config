-- state/schema.sql
--
-- Canonical snapshot of the SDLC ledger schema (Epic-04, Story 4.1-001).
--
-- This file documents the FULL target shape of `.sdlc-state.db` after all
-- migrations in `state/migrations/` have been applied. It is NOT applied
-- directly by `scripts/sdlc-state.sh init` — that script runs the migration
-- chain so that a fresh DB and an evolved DB converge on the same schema and
-- so that `_migrations` is populated identically in both cases.
--
-- Treat this file as the human-readable reference. To change the schema,
-- write a new `state/migrations/NNN-<name>.sql`. The CI / bats suite verifies
-- that the migration chain produces exactly the tables and columns below.
--
-- Consumed by:
--   * Story 4.2-001 (write path: orchestrator + agents INSERT/UPDATE here).
--   * Story 4.2-002 (markdown view: SELECT-only renderer).
--   * Story 4.3-001 (resume: SELECT story/stage state, INSERT next attempt).
--
-- Concurrency: WAL journal mode is enabled by `sdlc-state.sh init` (and by
-- migrate when it touches a fresh DB). Single-writer pattern is enforced at
-- the application layer (Story 4.2-001) — readers can run concurrently.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- runs: one row per `build-stories` invocation.
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,           -- UUID assigned at Phase 1.
    scope           TEXT,                       -- e.g. 'epic-04' or 'story:4.1-001'.
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    mode            TEXT,                       -- 'serial' | 'parallel'.
    total_stories   INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT NOT NULL               -- 'IN_PROGRESS' | 'DONE' | 'FAILED' | 'ABORTED'.
);

-- stories: one row per story scheduled within a run.
CREATE TABLE IF NOT EXISTS stories (
    run_id          TEXT NOT NULL,
    story_id        TEXT NOT NULL,              -- e.g. '4.1-001'.
    epic_id         TEXT,                       -- e.g. '04'.
    title           TEXT,
    priority        TEXT,                       -- 'P0' | 'P1' | 'P2' | 'P3'.
    points          INTEGER,
    agent_type      TEXT,                       -- e.g. 'bash-zsh-macos-engineer'.
    branch          TEXT,                       -- e.g. 'feature/4.1-001'.
    pr_number       INTEGER,
    current_stage   TEXT,                       -- last stage transitioned through.
    status          TEXT NOT NULL,              -- 'TODO' | 'IN_PROGRESS' | 'DONE' | 'FAILED' | 'BLOCKED'.
    PRIMARY KEY (run_id, story_id),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- stages: append-on-attempt log of per-stage transitions. The `attempt`
-- column lets Story 4.3-001 (resume) record retries without losing history.
CREATE TABLE IF NOT EXISTS stages (
    run_id              TEXT NOT NULL,
    story_id            TEXT NOT NULL,
    stage_name          TEXT NOT NULL,          -- e.g. 'build', 'review', 'merge', 'cleanup'.
    attempt             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,          -- 'IN_PROGRESS' | 'DONE' | 'FAILED' | 'STALE' | 'SKIPPED'.
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    failure_category    TEXT,                   -- typed enum carried from agent output (e.g. 'flaky-test', 'merge-conflict').
    output_path         TEXT,                   -- optional path to a full transcript/log.
    PRIMARY KEY (run_id, story_id, stage_name, attempt),
    FOREIGN KEY (run_id, story_id) REFERENCES stories(run_id, story_id) ON DELETE CASCADE
);

-- events: cheap audit log, mirrors every `cmux-bridge log` call so we can
-- reconstruct a run timeline without scraping markdown.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    story_id    TEXT,
    ts          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level       TEXT NOT NULL,                  -- 'info' | 'warn' | 'error' | 'success' | 'debug' | 'progress'.
    source      TEXT,                           -- which agent or hook emitted it.
    message     TEXT NOT NULL,
    stage       TEXT,                           -- sub-stage progress: pipeline stage (Story 11.1-002).
    kind        TEXT                            -- sub-stage progress: agent_started|tool_use|file_changed|test_run|message.
);

-- _migrations: bookkeeping for the migration runner. `version` is the integer
-- prefix of the migration filename (e.g. `001-init.sql` → 1).
CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Lookup indexes used by Story 4.3-001 (resume) and the markdown renderer.
CREATE INDEX IF NOT EXISTS idx_stories_status      ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stages_status       ON stages(status);
CREATE INDEX IF NOT EXISTS idx_runs_status         ON runs(status);
CREATE INDEX IF NOT EXISTS idx_events_run_ts       ON events(run_id, ts);
