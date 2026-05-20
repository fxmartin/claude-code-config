#!/usr/bin/env bats
# Tests for the write path of scripts/sdlc-state.sh and hooks/sdlc-state-emit.sh
# (Story 4.2-001 — orchestrator and agents write to the ledger).
#
# These tests pin the write-API contract that the build-stories orchestrator
# and dispatched agents rely on. Every test uses an isolated BATS_TEST_TMPDIR
# DB so the real `.sdlc-state.db` is never touched.
#
# Pattern: each write subcommand is exposed via `sdlc-state.sh` and accepts
# `--db <path>` so tests can target a temp DB. The shared helper
# `hooks/sdlc-state-emit.sh` is a thin wrapper that locates `sdlc-state.sh`
# and forwards args, so agents do not embed paths.

SDLC_STATE="${BATS_TEST_DIRNAME}/../scripts/sdlc-state.sh"
EMIT_HOOK="${BATS_TEST_DIRNAME}/../hooks/sdlc-state-emit.sh"

setup() {
    DB="${BATS_TEST_TMPDIR}/test.db"
    "${SDLC_STATE}" --db "${DB}" init >/dev/null
}

# ---------------------------------------------------------------------------
# run-create: INSERT INTO runs and return the run_id
# ---------------------------------------------------------------------------

@test "run-create inserts a row and prints the run_id" {
    run "${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel
    [ "${status}" -eq 0 ]
    # Output is the run_id (a non-empty token).
    [ -n "${output}" ]
    local run_id="${output}"
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE id='${run_id}';")
    [ "${count}" = "1" ]
}

@test "run-create records scope, mode, status=IN_PROGRESS, and started_at" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT scope, mode, status, started_at IS NOT NULL FROM runs WHERE id='${run_id}';")
    [ "${row}" = "epic-04|parallel|IN_PROGRESS|1" ]
}

@test "run-create with embedded single-quotes in scope is safely parameterized" {
    # The scope is a free-text label — must not be interpolated as raw SQL.
    local nasty="epic'); DROP TABLE runs;--"
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create "${nasty}" serial)
    # Both the runs table and the row must survive intact.
    local tables count
    tables=$(sqlite3 "${DB}" "SELECT name FROM sqlite_master WHERE type='table';")
    [[ "${tables}" == *"runs"* ]]
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE id='${run_id}';")
    [ "${count}" = "1" ]
    # The scope round-trips exactly.
    local stored
    stored=$(sqlite3 "${DB}" "SELECT scope FROM runs WHERE id='${run_id}';")
    [ "${stored}" = "${nasty}" ]
}

@test "run-create rejects missing arguments" {
    run "${SDLC_STATE}" --db "${DB}" run-create
    [ "${status}" -ne 0 ]
    run "${SDLC_STATE}" --db "${DB}" run-create epic-04
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# run-update-status: change runs.status (and finished_at for terminal states)
# ---------------------------------------------------------------------------

@test "run-update-status transitions IN_PROGRESS -> DONE and stamps finished_at" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    run "${SDLC_STATE}" --db "${DB}" run-update-status "${run_id}" DONE
    [ "${status}" -eq 0 ]
    local status_col finished
    status_col=$(sqlite3 "${DB}" "SELECT status FROM runs WHERE id='${run_id}';")
    finished=$(sqlite3 "${DB}" "SELECT finished_at IS NOT NULL FROM runs WHERE id='${run_id}';")
    [ "${status_col}" = "DONE" ]
    [ "${finished}" = "1" ]
}

@test "run-update-status to IN_PROGRESS leaves finished_at NULL" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${run_id}" IN_PROGRESS
    local finished
    finished=$(sqlite3 "${DB}" "SELECT finished_at IS NULL FROM runs WHERE id='${run_id}';")
    [ "${finished}" = "1" ]
}

@test "run-update-status on unknown run_id exits non-zero" {
    run "${SDLC_STATE}" --db "${DB}" run-update-status no-such-run DONE
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# story-upsert: INSERT OR REPLACE story row
# ---------------------------------------------------------------------------

@test "story-upsert inserts a fresh story row" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    run "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    [ "${status}" -eq 0 ]
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT story_id, epic_id, title, priority, points, agent_type, branch, status
           FROM stories WHERE run_id='${run_id}';")
    [ "${row}" = "4.2-001|04|Write path|P1|5|bash-zsh-macos-engineer|feature/4.2-001|IN_PROGRESS" ]
}

@test "story-upsert replaces an existing row (idempotent upsert)" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 99 DONE
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT pr_number, status FROM stories WHERE run_id='${run_id}' AND story_id='4.2-001';")
    [ "${row}" = "99|DONE" ]
    # Still exactly one row.
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stories WHERE run_id='${run_id}' AND story_id='4.2-001';")
    [ "${count}" = "1" ]
}

@test "story-upsert with a title containing single quotes round-trips intact" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    local title="FX's tricky title with 'embedded' quotes"
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "${title}" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    local stored
    stored=$(sqlite3 "${DB}" "SELECT title FROM stories WHERE run_id='${run_id}';")
    [ "${stored}" = "${title}" ]
}

@test "story-upsert respects FK: unknown run_id is rejected" {
    run "${SDLC_STATE}" --db "${DB}" story-upsert \
        no-such-run 4.2-001 04 "X" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# stage-start: append IN_PROGRESS row for (run, story, stage, attempt)
# ---------------------------------------------------------------------------

@test "stage-start appends an IN_PROGRESS stage row with started_at set" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    run "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" 4.2-001 build 1
    [ "${status}" -eq 0 ]
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT stage_name, attempt, status, started_at IS NOT NULL, finished_at IS NULL
           FROM stages WHERE run_id='${run_id}' AND story_id='4.2-001';")
    [ "${row}" = "build|1|IN_PROGRESS|1|1" ]
}

@test "stage-start with a non-existent (run, story) pair exits non-zero (FK)" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    # No story upserted — the FK from stages -> stories must reject.
    run "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" 4.2-001 build 1
    [ "${status}" -ne 0 ]
}

@test "stage-start defaults attempt to 1 when omitted" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" 4.2-001 build
    local attempt
    attempt=$(sqlite3 "${DB}" \
        "SELECT attempt FROM stages WHERE run_id='${run_id}' AND story_id='4.2-001' AND stage_name='build';")
    [ "${attempt}" = "1" ]
}

# ---------------------------------------------------------------------------
# stage-finish: UPDATE existing IN_PROGRESS stage to a terminal status
# ---------------------------------------------------------------------------

@test "stage-finish flips IN_PROGRESS -> DONE and stamps finished_at" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" 4.2-001 build 1
    run "${SDLC_STATE}" --db "${DB}" stage-finish "${run_id}" 4.2-001 build 1 DONE "" ""
    [ "${status}" -eq 0 ]
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT status, finished_at IS NOT NULL, failure_category, output_path
           FROM stages WHERE run_id='${run_id}' AND story_id='4.2-001' AND stage_name='build' AND attempt=1;")
    [ "${row}" = "DONE|1||" ]
}

@test "stage-finish records failure_category and output_path on FAILED" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" 4.2-001 coverage 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${run_id}" 4.2-001 coverage 1 FAILED flaky-test /tmp/log
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT status, failure_category, output_path
           FROM stages WHERE run_id='${run_id}' AND story_id='4.2-001' AND stage_name='coverage' AND attempt=1;")
    [ "${row}" = "FAILED|flaky-test|/tmp/log" ]
}

@test "stage-finish on a non-existent stage row exits non-zero" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    # No stage-start first.
    run "${SDLC_STATE}" --db "${DB}" stage-finish "${run_id}" 4.2-001 build 1 DONE "" ""
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# event-log: cheap append into events
# ---------------------------------------------------------------------------

@test "event-log appends an events row with level + source + message" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    run "${SDLC_STATE}" --db "${DB}" event-log "${run_id}" 4.2-001 info build-stories "kicking off cohort 1"
    [ "${status}" -eq 0 ]
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT level, source, message FROM events WHERE run_id='${run_id}';")
    [ "${row}" = "info|build-stories|kicking off cohort 1" ]
}

@test "event-log accepts an empty story_id (run-level events)" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" event-log "${run_id}" "" success build-stories "preflight green"
    local row
    row=$(sqlite3 -separator '|' "${DB}" \
        "SELECT story_id, level, message FROM events WHERE run_id='${run_id}';")
    [ "${row}" = "|success|preflight green" ]
}

@test "event-log with a message containing single quotes round-trips intact" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    local msg="failed: can't connect to 'remote'"
    "${SDLC_STATE}" --db "${DB}" event-log "${run_id}" 4.2-001 error build-stories "${msg}"
    local stored
    stored=$(sqlite3 "${DB}" "SELECT message FROM events WHERE run_id='${run_id}';")
    [ "${stored}" = "${msg}" ]
}

# ---------------------------------------------------------------------------
# End-to-end: a simulated 3-story 4-stage run produces expected row counts.
# This mirrors the Definition of Done for story 4.2-001.
# ---------------------------------------------------------------------------

@test "simulated 3-story 4-stage run produces 1 run, 3 stories, 12 stages, >=N events" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    local s
    for s in 4.2-A 4.2-B 4.2-C; do
        "${SDLC_STATE}" --db "${DB}" story-upsert \
            "${run_id}" "${s}" 04 "Story ${s}" P1 3 bash-zsh-macos-engineer "feature/${s}" "" IN_PROGRESS
        local stg
        for stg in build coverage review merge; do
            "${SDLC_STATE}" --db "${DB}" stage-start "${run_id}" "${s}" "${stg}" 1
            "${SDLC_STATE}" --db "${DB}" stage-finish "${run_id}" "${s}" "${stg}" 1 DONE "" ""
            "${SDLC_STATE}" --db "${DB}" event-log "${run_id}" "${s}" info build-stories "${stg} done"
        done
        "${SDLC_STATE}" --db "${DB}" story-upsert \
            "${run_id}" "${s}" 04 "Story ${s}" P1 3 bash-zsh-macos-engineer "feature/${s}" 100 DONE
    done
    "${SDLC_STATE}" --db "${DB}" run-update-status "${run_id}" DONE

    local runs stories stages events
    runs=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs;")
    stories=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stories WHERE run_id='${run_id}';")
    stages=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stages WHERE run_id='${run_id}';")
    events=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM events WHERE run_id='${run_id}';")

    [ "${runs}" = "1" ]
    [ "${stories}" = "3" ]
    [ "${stages}" = "12" ]
    [ "${events}" = "12" ]
}

# ---------------------------------------------------------------------------
# hooks/sdlc-state-emit.sh — thin wrapper used by agents.
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh is executable" {
    [ -x "${EMIT_HOOK}" ]
}

@test "sdlc-state-emit.sh forwards run-create to sdlc-state.sh" {
    SDLC_STATE_DB="${DB}" run "${EMIT_HOOK}" run-create epic-04 parallel
    [ "${status}" -eq 0 ]
    local run_id="${output}"
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE id='${run_id}';")
    [ "${count}" = "1" ]
}

@test "sdlc-state-emit.sh forwards stage-start + stage-finish through the same DB" {
    SDLC_STATE_DB="${DB}" local run_id
    run_id=$(SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" run-create epic-04 parallel)
    SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" story-upsert \
        "${run_id}" 4.2-001 04 "Write path" P1 5 bash-zsh-macos-engineer feature/4.2-001 "" IN_PROGRESS
    SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" stage-start "${run_id}" 4.2-001 build 1
    SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" stage-finish "${run_id}" 4.2-001 build 1 DONE "" ""
    local status_col
    status_col=$(sqlite3 "${DB}" "SELECT status FROM stages WHERE run_id='${run_id}';")
    [ "${status_col}" = "DONE" ]
}

@test "sdlc-state-emit.sh is silent when SDLC_STATE_DB is empty (graceful no-op)" {
    # Mirrors cmux-bridge.sh's graceful-degradation pattern: when the orchestrator
    # has not initialised a ledger, emit calls must succeed silently so agents
    # do not blow up in environments where SQLite tracking is disabled.
    unset SDLC_STATE_DB
    run "${EMIT_HOOK}" event-log "${BATS_TEST_TMPDIR}/nope" 4.2-001 info build-stories "no-op"
    [ "${status}" -eq 0 ]
    # And no DB was created.
    [ ! -f "${BATS_TEST_TMPDIR}/nope" ]
}
