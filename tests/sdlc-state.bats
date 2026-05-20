#!/usr/bin/env bats
# Tests for scripts/sdlc-state.sh and state/schema.sql (Story 4.1-001).
#
# This is the foundation story for Epic-04 (Durable State with SQLite). The
# schema is consumed by stories 4.2-001 (write path), 4.2-002 (markdown view),
# and 4.3-001 (resume from ledger), so the table shapes pinned here are
# load-bearing — breaking them in a later story forces a migration.
#
# Strategy: every test uses an isolated `BATS_TEST_TMPDIR` directory so the
# real `.sdlc-state.db` is never touched. The CLI is invoked with explicit
# `--db <path>` so we never depend on cwd or HOME.

SDLC_STATE="${BATS_TEST_DIRNAME}/../scripts/sdlc-state.sh"
SCHEMA="${BATS_TEST_DIRNAME}/../state/schema.sql"
MIGRATIONS_DIR="${BATS_TEST_DIRNAME}/../state/migrations"

setup() {
    DB="${BATS_TEST_TMPDIR}/test.db"
}

# --- Schema and tooling presence ------------------------------------------

@test "schema.sql exists at state/schema.sql" {
    [ -f "${SCHEMA}" ]
}

@test "state/migrations directory exists with at least one migration" {
    [ -d "${MIGRATIONS_DIR}" ]
    # At least one NNN-<name>.sql file
    local count
    count=$(find "${MIGRATIONS_DIR}" -maxdepth 1 -type f -name '[0-9][0-9][0-9]-*.sql' | wc -l | tr -d ' ')
    [ "${count}" -ge 1 ]
}

@test "sdlc-state.sh is executable" {
    [ -x "${SDLC_STATE}" ]
}

@test "sdlc-state.sh without args prints usage and exits non-zero" {
    run "${SDLC_STATE}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"Usage"* ]] || [[ "${output}" == *"usage"* ]]
}

# --- init: apply schema cleanly to a fresh DB -----------------------------

@test "init creates DB file and applies schema" {
    run "${SDLC_STATE}" --db "${DB}" init
    [ "${status}" -eq 0 ]
    [ -f "${DB}" ]
}

@test "init enables WAL journal mode" {
    "${SDLC_STATE}" --db "${DB}" init
    local mode
    mode=$(sqlite3 "${DB}" "PRAGMA journal_mode;")
    [ "${mode}" = "wal" ]
}

@test "init creates the required tables: runs, stories, stages, events, _migrations" {
    "${SDLC_STATE}" --db "${DB}" init
    local tables
    tables=$(sqlite3 "${DB}" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    [[ "${tables}" == *"runs"* ]]
    [[ "${tables}" == *"stories"* ]]
    [[ "${tables}" == *"stages"* ]]
    [[ "${tables}" == *"events"* ]]
    [[ "${tables}" == *"_migrations"* ]]
}

@test "runs table has the columns required by Epic-04 stories 4.2-001 and 4.3-001" {
    "${SDLC_STATE}" --db "${DB}" init
    local cols
    cols=$(sqlite3 "${DB}" "PRAGMA table_info(runs);" | awk -F'|' '{print $2}' | sort | paste -sd, -)
    # Required: id, scope, started_at, finished_at, mode, total_stories, completed, failed, status
    [[ "${cols}" == *"id"* ]]
    [[ "${cols}" == *"scope"* ]]
    [[ "${cols}" == *"started_at"* ]]
    [[ "${cols}" == *"finished_at"* ]]
    [[ "${cols}" == *"mode"* ]]
    [[ "${cols}" == *"total_stories"* ]]
    [[ "${cols}" == *"completed"* ]]
    [[ "${cols}" == *"failed"* ]]
    [[ "${cols}" == *"status"* ]]
}

@test "stories table has the columns required by Epic-04 stories 4.2-001 and 4.3-001" {
    "${SDLC_STATE}" --db "${DB}" init
    local cols
    cols=$(sqlite3 "${DB}" "PRAGMA table_info(stories);" | awk -F'|' '{print $2}' | sort | paste -sd, -)
    [[ "${cols}" == *"run_id"* ]]
    [[ "${cols}" == *"story_id"* ]]
    [[ "${cols}" == *"epic_id"* ]]
    [[ "${cols}" == *"title"* ]]
    [[ "${cols}" == *"priority"* ]]
    [[ "${cols}" == *"points"* ]]
    [[ "${cols}" == *"agent_type"* ]]
    [[ "${cols}" == *"branch"* ]]
    [[ "${cols}" == *"pr_number"* ]]
    [[ "${cols}" == *"current_stage"* ]]
    [[ "${cols}" == *"status"* ]]
}

@test "stages table has the columns required by Epic-04 story 4.3-001 (attempt + failure_category)" {
    "${SDLC_STATE}" --db "${DB}" init
    local cols
    cols=$(sqlite3 "${DB}" "PRAGMA table_info(stages);" | awk -F'|' '{print $2}' | sort | paste -sd, -)
    [[ "${cols}" == *"run_id"* ]]
    [[ "${cols}" == *"story_id"* ]]
    [[ "${cols}" == *"stage_name"* ]]
    [[ "${cols}" == *"attempt"* ]]
    [[ "${cols}" == *"status"* ]]
    [[ "${cols}" == *"started_at"* ]]
    [[ "${cols}" == *"finished_at"* ]]
    [[ "${cols}" == *"failure_category"* ]]
    [[ "${cols}" == *"output_path"* ]]
}

@test "events table is an autoincrement append log" {
    "${SDLC_STATE}" --db "${DB}" init
    local cols
    cols=$(sqlite3 "${DB}" "PRAGMA table_info(events);" | awk -F'|' '{print $2}' | sort | paste -sd, -)
    [[ "${cols}" == *"id"* ]]
    [[ "${cols}" == *"run_id"* ]]
    [[ "${cols}" == *"story_id"* ]]
    [[ "${cols}" == *"ts"* ]]
    [[ "${cols}" == *"level"* ]]
    [[ "${cols}" == *"source"* ]]
    [[ "${cols}" == *"message"* ]]
}

@test "stories table has composite primary key (run_id, story_id)" {
    "${SDLC_STATE}" --db "${DB}" init
    # Inserting same (run_id, story_id) twice must conflict.
    sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, status) VALUES ('r1', 's1', 'TODO');"
    run sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, status) VALUES ('r1', 's1', 'TODO');"
    [ "${status}" -ne 0 ]
}

@test "stages table has composite primary key (run_id, story_id, stage_name, attempt)" {
    "${SDLC_STATE}" --db "${DB}" init
    sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('r1', 's1', 'build', 1, 'DONE');"
    # Same key conflicts.
    run sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('r1', 's1', 'build', 1, 'DONE');"
    [ "${status}" -ne 0 ]
    # Bumping attempt is allowed (this is exactly how 4.3-001 resume retries).
    run sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('r1', 's1', 'build', 2, 'IN_PROGRESS');"
    [ "${status}" -eq 0 ]
}

# --- migrate: idempotent re-application -----------------------------------

@test "migrate on a fresh DB applies all migrations and records them in _migrations" {
    run "${SDLC_STATE}" --db "${DB}" migrate
    [ "${status}" -eq 0 ]
    local applied
    applied=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    [ "${applied}" -ge 1 ]
}

@test "migrate is idempotent: re-running applies zero new migrations" {
    "${SDLC_STATE}" --db "${DB}" migrate
    local first_count
    first_count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    run "${SDLC_STATE}" --db "${DB}" migrate
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"up to date"* ]] || [[ "${output}" == *"0 applied"* ]] || [[ "${output}" == *"no pending"* ]]
    local second_count
    second_count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    [ "${first_count}" = "${second_count}" ]
}

@test "init followed by migrate is a no-op (init applies all migrations too)" {
    "${SDLC_STATE}" --db "${DB}" init
    local before
    before=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    run "${SDLC_STATE}" --db "${DB}" migrate
    [ "${status}" -eq 0 ]
    local after
    after=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    [ "${before}" = "${after}" ]
}

# --- show: inspect a run --------------------------------------------------

@test "show <run-id> on an unknown run exits non-zero with a clear message" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" show nonexistent-run
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not found"* ]] || [[ "${output}" == *"no such run"* ]]
}

@test "show <run-id> prints the run summary and its stories" {
    "${SDLC_STATE}" --db "${DB}" init
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status, total_stories) VALUES ('run-42', 'epic-04', 'parallel', 'IN_PROGRESS', 2);"
    sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, epic_id, title, status) VALUES ('run-42', '4.1-001', '04', 'Schema', 'DONE');"
    sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, epic_id, title, status) VALUES ('run-42', '4.2-001', '04', 'Write path', 'TODO');"
    run "${SDLC_STATE}" --db "${DB}" show run-42
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"run-42"* ]]
    [[ "${output}" == *"4.1-001"* ]]
    [[ "${output}" == *"4.2-001"* ]]
}

# --- backup: copy the ledger ----------------------------------------------

@test "backup writes a copy of the DB to the given path" {
    "${SDLC_STATE}" --db "${DB}" init
    local backup="${BATS_TEST_TMPDIR}/test.db.bak"
    run "${SDLC_STATE}" --db "${DB}" backup "${backup}"
    [ "${status}" -eq 0 ]
    [ -f "${backup}" ]
    # Backup is a valid SQLite file with the same tables.
    local tables
    tables=$(sqlite3 "${backup}" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    [[ "${tables}" == *"runs"* ]]
    [[ "${tables}" == *"_migrations"* ]]
}

# --- prune: cull old runs --------------------------------------------------

@test "prune --older-than removes runs whose finished_at is older than the cutoff" {
    "${SDLC_STATE}" --db "${DB}" init
    # Old finished run (10 days ago) and a fresh one (just finished).
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status, finished_at) VALUES ('old', 'epic', 'parallel', 'DONE', datetime('now','-10 days'));"
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status, finished_at) VALUES ('new', 'epic', 'parallel', 'DONE', datetime('now','-1 hours'));"
    run "${SDLC_STATE}" --db "${DB}" prune --older-than 7d
    [ "${status}" -eq 0 ]
    local remaining
    remaining=$(sqlite3 "${DB}" "SELECT id FROM runs ORDER BY id;")
    [[ "${remaining}" != *"old"* ]]
    [[ "${remaining}" == *"new"* ]]
}

@test "prune leaves IN_PROGRESS runs alone regardless of age" {
    "${SDLC_STATE}" --db "${DB}" init
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status, started_at, finished_at) VALUES ('orphan', 'epic', 'parallel', 'IN_PROGRESS', datetime('now','-30 days'), NULL);"
    run "${SDLC_STATE}" --db "${DB}" prune --older-than 7d
    [ "${status}" -eq 0 ]
    local remaining
    remaining=$(sqlite3 "${DB}" "SELECT id FROM runs;")
    [[ "${remaining}" == *"orphan"* ]]
}

# --- .gitignore guard ------------------------------------------------------

@test ".sdlc-state.db is in .gitignore" {
    grep -q '^\.sdlc-state\.db' "${BATS_TEST_DIRNAME}/../.gitignore"
}
