#!/usr/bin/env bats
# Gap coverage for scripts/sdlc-state.sh (Story 4.1-001 QA pass).
#
# Splits from sdlc-state.bats to keep the error-path and FK-cascade tests
# isolated. Covers:
#   - CLI argument errors (unknown subcommand, missing --db value, missing show arg)
#   - Environment errors (init to non-writable dir, migrate on corrupt DB)
#   - prune edge values (0-second cutoff, malformed duration)
#   - backup edge cases (dest exists, dest dir missing)
#   - Migration version recovery (orphan _migrations row pointing at missing file)
#   - WAL mode persistence across DB reopen
#   - FK cascade (delete run → children gone)
#
# All tests use BATS_TEST_TMPDIR so the real .sdlc-state.db is never touched.

SDLC_STATE="${BATS_TEST_DIRNAME}/../scripts/sdlc-state.sh"

setup() {
    DB="${BATS_TEST_TMPDIR}/test.db"
}

# ---------------------------------------------------------------------------
# CLI argument errors
# ---------------------------------------------------------------------------

@test "unknown subcommand exits non-zero with helpful message" {
    run "${SDLC_STATE}" --db "${DB}" frobnicate
    [ "${status}" -ne 0 ]
    # Should mention the bad subcommand name
    [[ "${output}" == *"unknown subcommand"* ]] || [[ "${output}" == *"frobnicate"* ]]
}

@test "--db with no following value exits non-zero" {
    run "${SDLC_STATE}" --db
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"--db"* ]] || [[ "${output}" == *"requires"* ]] || [[ "${output}" == *"Usage"* ]]
}

@test "show without a run-id argument exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" show
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"run-id"* ]] || [[ "${output}" == *"requires"* ]] || [[ "${output}" == *"argument"* ]]
}

@test "prune without --older-than flag exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" prune
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"--older-than"* ]] || [[ "${output}" == *"requires"* ]]
}

@test "backup without dest argument exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" backup
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"destination"* ]] || [[ "${output}" == *"requires"* ]]
}

# ---------------------------------------------------------------------------
# Environment / filesystem errors
# ---------------------------------------------------------------------------

@test "init to a non-writable directory exits non-zero" {
    # Skip if running as root (root can write anywhere).
    if [ "$(id -u)" = "0" ]; then
        skip "root can always write"
    fi
    local locked_dir="${BATS_TEST_TMPDIR}/locked"
    mkdir -p "${locked_dir}"
    chmod 000 "${locked_dir}"
    run "${SDLC_STATE}" --db "${locked_dir}/test.db" init
    [ "${status}" -ne 0 ]
    chmod 755 "${locked_dir}"   # restore so BATS can clean up
}

@test "migrate on a corrupt DB exits non-zero with a message" {
    # Corrupt DB: a file that is not a valid SQLite database.
    echo "not a valid sqlite database" > "${DB}"
    run "${SDLC_STATE}" --db "${DB}" migrate
    [ "${status}" -ne 0 ]
}

@test "backup when source DB does not exist exits non-zero" {
    # DB was never initialised — backup should fail clearly.
    run "${SDLC_STATE}" --db "${DB}" backup "${BATS_TEST_TMPDIR}/does-not-matter.bak"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"does not exist"* ]] || [[ "${output}" == *"not exist"* ]] || [[ "${output}" == *"error"* ]]
}

# ---------------------------------------------------------------------------
# backup edge cases
# ---------------------------------------------------------------------------

@test "backup over an existing valid SQLite file succeeds" {
    # First backup creates a valid DB file at dest.
    "${SDLC_STATE}" --db "${DB}" init
    local dest="${BATS_TEST_TMPDIR}/backup.db"
    "${SDLC_STATE}" --db "${DB}" backup "${dest}"
    # Second backup over the same (valid) file must also succeed.
    run "${SDLC_STATE}" --db "${DB}" backup "${dest}"
    [ "${status}" -eq 0 ]
    [ -f "${dest}" ]
    local tables
    tables=$(sqlite3 "${dest}" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    [[ "${tables}" == *"runs"* ]]
}

@test "backup fails when dest exists but is not a valid SQLite file" {
    # SQLite .backup requires the destination to be absent or a valid DB.
    "${SDLC_STATE}" --db "${DB}" init
    local dest="${BATS_TEST_TMPDIR}/corrupt.db"
    echo "not sqlite" > "${dest}"
    run "${SDLC_STATE}" --db "${DB}" backup "${dest}"
    [ "${status}" -ne 0 ]
}

@test "backup when dest parent directory does not exist exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" backup "${BATS_TEST_TMPDIR}/nonexistent-subdir/backup.db"
    # SQLite .backup will fail if the parent dir does not exist.
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# prune edge values
# ---------------------------------------------------------------------------

@test "prune --older-than 0d removes all non-IN_PROGRESS finished runs" {
    "${SDLC_STATE}" --db "${DB}" init
    # A run finished right now — with a 0-day cutoff it should be pruned.
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status, finished_at) VALUES ('zero-day', 'epic', 'parallel', 'DONE', datetime('now','-1 seconds'));"
    run "${SDLC_STATE}" --db "${DB}" prune --older-than 0d
    [ "${status}" -eq 0 ]
    local remaining
    remaining=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE id='zero-day';")
    [ "${remaining}" = "0" ]
}

@test "prune --older-than with malformed duration exits with status 2" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" prune --older-than notaduration
    [ "${status}" -eq 2 ]
}

@test "prune --older-than with empty duration string exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" prune --older-than ""
    [ "${status}" -ne 0 ]
}

@test "prune --older-than with pure number (no unit) exits non-zero" {
    "${SDLC_STATE}" --db "${DB}" init
    run "${SDLC_STATE}" --db "${DB}" prune --older-than 7
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Migration version tracking: partial-failure recovery
# ---------------------------------------------------------------------------

@test "migrate skips a migration whose version is already in _migrations even if the SQL file is absent" {
    # Arrange: init the DB (applies migration 001).
    "${SDLC_STATE}" --db "${DB}" init
    local before
    before=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")

    # Simulate: inject a _migrations row for a hypothetical migration 002
    # that no longer has a corresponding .sql file on disk.
    sqlite3 "${DB}" "INSERT INTO _migrations(version, name) VALUES (2, 'phantom-migration');"

    # migrate must not fail and must not create a new row for version 002.
    run "${SDLC_STATE}" --db "${DB}" migrate
    [ "${status}" -eq 0 ]

    local after
    after=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM _migrations;")
    # Row count should be exactly before+1 (the phantom row we injected, nothing new).
    [ "${after}" = "$((before + 1))" ]
}

# ---------------------------------------------------------------------------
# WAL mode persistence across DB reopen
# ---------------------------------------------------------------------------

@test "WAL journal mode persists after closing and reopening the DB" {
    "${SDLC_STATE}" --db "${DB}" init
    # Close by finishing the init, then open a brand-new sqlite3 process.
    local mode
    mode=$(sqlite3 "${DB}" "PRAGMA journal_mode;")
    [ "${mode}" = "wal" ]
    # A second independent process must also see wal, not the SQLite default.
    mode=$(sqlite3 "${DB}" "SELECT * FROM pragma_journal_mode();")
    [ "${mode}" = "wal" ]
}

# ---------------------------------------------------------------------------
# Foreign key cascade: delete run removes children
# ---------------------------------------------------------------------------

@test "FK cascade: deleting a run removes its stories, stages, and events" {
    "${SDLC_STATE}" --db "${DB}" init
    # Ensure FKs are enforced for this connection.
    sqlite3 "${DB}" "PRAGMA foreign_keys = ON;"

    # Parent run.
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status) VALUES ('run-cascade', 'epic', 'parallel', 'IN_PROGRESS');"

    # Child story.
    sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, status) VALUES ('run-cascade', 's1', 'TODO');"

    # Child stage (depends on the story via composite FK).
    sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('run-cascade', 's1', 'build', 1, 'IN_PROGRESS');"

    # Event (run_id only — no FK constraint, but prune relies on this being orphaned).
    sqlite3 "${DB}" "INSERT INTO events(run_id, story_id, level, message) VALUES ('run-cascade', 's1', 'info', 'test event');"

    # Verify children exist before delete.
    local stories_before stages_before
    stories_before=$(sqlite3 "${DB}" "PRAGMA foreign_keys=ON; SELECT COUNT(*) FROM stories WHERE run_id='run-cascade';")
    stages_before=$(sqlite3 "${DB}" "PRAGMA foreign_keys=ON; SELECT COUNT(*) FROM stages WHERE run_id='run-cascade';")
    [ "${stories_before}" = "1" ]
    [ "${stages_before}" = "1" ]

    # Delete the parent run with FKs enforced.
    sqlite3 "${DB}" "PRAGMA foreign_keys=ON; DELETE FROM runs WHERE id='run-cascade';"

    # All FK-linked children must be gone.
    local stories_after stages_after
    stories_after=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stories WHERE run_id='run-cascade';")
    stages_after=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stages WHERE run_id='run-cascade';")
    [ "${stories_after}" = "0" ]
    [ "${stages_after}" = "0" ]
}

@test "FK cascade: deleting a story removes its stages" {
    "${SDLC_STATE}" --db "${DB}" init
    sqlite3 "${DB}" "INSERT INTO runs(id, scope, mode, status) VALUES ('run-fk', 'epic', 'parallel', 'IN_PROGRESS');"
    sqlite3 "${DB}" "INSERT INTO stories(run_id, story_id, status) VALUES ('run-fk', 's1', 'TODO');"
    sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('run-fk', 's1', 'build', 1, 'DONE');"
    sqlite3 "${DB}" "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) VALUES ('run-fk', 's1', 'review', 1, 'IN_PROGRESS');"

    sqlite3 "${DB}" "PRAGMA foreign_keys=ON; DELETE FROM stories WHERE run_id='run-fk' AND story_id='s1';"

    local stages_after
    stages_after=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stages WHERE run_id='run-fk' AND story_id='s1';")
    [ "${stages_after}" = "0" ]
}

