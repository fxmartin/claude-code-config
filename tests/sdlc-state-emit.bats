#!/usr/bin/env bats
# Tests for hooks/sdlc-state-emit.sh edge cases (Story 4.2-001).
#
# The emit hook is the agent-facing facade: it locates sdlc-state.sh,
# resolves the DB path, and degrades gracefully when neither the env var
# nor a known ledger file can be found.  These tests cover the degradation
# paths and failure modes that are NOT exercised in sdlc-state-write.bats.

SDLC_STATE="${BATS_TEST_DIRNAME}/../scripts/sdlc-state.sh"
EMIT_HOOK="${BATS_TEST_DIRNAME}/../hooks/sdlc-state-emit.sh"

setup() {
    DB="${BATS_TEST_TMPDIR}/test.db"
    "${SDLC_STATE}" --db "${DB}" init >/dev/null
}

# ---------------------------------------------------------------------------
# Graceful degradation: SDLC_STATE_DB pointing to a corrupt (non-SQLite) file.
# The emit hook forwards to sdlc-state.sh which will fail to parse the DB.
# The hook MUST exit non-zero when an explicit DB path is given but the file is
# corrupt — silent degradation is reserved for the "no DB configured" case.
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh with SDLC_STATE_DB set to a corrupt file exits non-zero" {
    local corrupt="${BATS_TEST_TMPDIR}/corrupt.db"
    # Write random bytes — not a valid SQLite file.
    printf '\x00\x01\x02\x03\xFF\xFE garbage data, not sqlite' > "${corrupt}"
    run env SDLC_STATE_DB="${corrupt}" "${EMIT_HOOK}" \
        event-log "fake-run" "" info build-stories "test"
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Graceful degradation: SDLC_STATE_DB is unset AND there is no .sdlc-state.db
# in cwd or repo root.  The hook must exit 0 silently (no file created).
# (Mirrors the existing test in sdlc-state-write.bats test 66 but from cwd
# isolation so we confirm the "no-op on unresolvable path" branch.)
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh without SDLC_STATE_DB and no ledger file is a silent no-op" {
    # Run from a temp dir with no .sdlc-state.db and no $SDLC_STATE_DB.
    local tmpdir="${BATS_TEST_TMPDIR}/norepo"
    mkdir -p "${tmpdir}"
    unset SDLC_STATE_DB
    run env -i HOME="${HOME}" PATH="${PATH}" \
        bash -c "cd '${tmpdir}' && '${EMIT_HOOK}' event-log 'x' '' info src 'msg'"
    [ "${status}" -eq 0 ]
    # No DB file was auto-created.
    [ ! -f "${tmpdir}/.sdlc-state.db" ]
}

# ---------------------------------------------------------------------------
# Graceful degradation: SDLC_STATE_DB is set to a path whose parent directory
# does not exist.  sdlc-state.sh will fail to init; the hook should exit
# non-zero (not silently swallow the error) because an explicit path was given.
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh with SDLC_STATE_DB pointing to a non-existent dir exits non-zero" {
    local bad_path="${BATS_TEST_TMPDIR}/no/such/dir/test.db"
    run env SDLC_STATE_DB="${bad_path}" "${EMIT_HOOK}" \
        event-log "fake-run" "" info build-stories "test"
    [ "${status}" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Fallback to cwd .sdlc-state.db when SDLC_STATE_DB is unset but a ledger
# file exists in cwd (priority 3 in _resolve_db_path).
# We change directory to BATS_TEST_TMPDIR where we place a pre-initialised DB,
# then verify a write succeeds without setting SDLC_STATE_DB.
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh falls back to cwd .sdlc-state.db when env var is unset" {
    # Place an initialised ledger in a temp dir.
    local ledger_dir="${BATS_TEST_TMPDIR}/fallback"
    mkdir -p "${ledger_dir}"
    local ledger="${ledger_dir}/.sdlc-state.db"
    "${SDLC_STATE}" --db "${ledger}" init >/dev/null
    # Insert a run directly so we have a valid run_id.
    local run_id
    run_id=$("${SDLC_STATE}" --db "${ledger}" run-create epic-fallback parallel)
    # Use env -i to strip SDLC_STATE_DB from the environment, then run from
    # the ledger_dir so the cwd discovery branch fires.
    unset SDLC_STATE_DB
    run bash -c "cd '${ledger_dir}' && '${EMIT_HOOK}' event-log '${run_id}' '' info emit-test 'cwd fallback'"
    [ "${status}" -eq 0 ]
    # Verify the event was actually written.
    local count
    count=$(sqlite3 "${ledger}" "SELECT COUNT(*) FROM events WHERE run_id='${run_id}';")
    [ "${count}" = "1" ]
}

# ---------------------------------------------------------------------------
# emit with no args: should print usage and exit 1 (not crash).
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh with no arguments exits 1 with usage" {
    run "${EMIT_HOOK}"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"Usage"* ]]
}

# ---------------------------------------------------------------------------
# Injection: emit hook propagates sql_quote protection end-to-end.
# The agent-facing hook must never let a crafted event message corrupt the DB.
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh: event-log message with injection payload is safe end-to-end" {
    local run_id
    run_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    local nasty="msg'); DROP TABLE events;--"
    SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" event-log "${run_id}" "" warn emit-test "${nasty}"
    # events table must still exist.
    local tables
    tables=$(sqlite3 "${DB}" "SELECT name FROM sqlite_master WHERE type='table' AND name='events';")
    [ "${tables}" = "events" ]
    # Message round-trips exactly.
    local stored
    stored=$(sqlite3 "${DB}" "SELECT message FROM events WHERE run_id='${run_id}';")
    [ "${stored}" = "${nasty}" ]
}

# ---------------------------------------------------------------------------
# emit run-create prints the run_id and it is captured correctly by the
# orchestrator pattern: SDLC_RUN_ID=$(sdlc-state-emit.sh run-create ...)
# ---------------------------------------------------------------------------

@test "sdlc-state-emit.sh run-create stdout is exactly the UUID (no trailing newline artifacts)" {
    local run_id
    run_id=$(SDLC_STATE_DB="${DB}" "${EMIT_HOOK}" run-create epic-04 parallel)
    # Must be non-empty and match the UUID-ish pattern (8-4-4-4-12 hex).
    [[ "${run_id}" =~ ^[0-9a-f-]{36}$ ]]
    # Must exist in the DB.
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE id='${run_id}';")
    [ "${count}" = "1" ]
}
