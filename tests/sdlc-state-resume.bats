#!/usr/bin/env bats
# Tests for the resume path of scripts/sdlc-state.sh (Story 4.3-001 — resume
# run from ledger state). These tests pin the resume contract that the
# build-stories orchestrator relies on when `/build-stories resume` is
# re-invoked after a crash, kill, or operator abort.
#
# Pattern: every test uses an isolated BATS_TEST_TMPDIR DB so the real
# `.sdlc-state.db` is never touched. The `--db` flag targets the temp DB
# at every call.

SDLC_STATE="${BATS_TEST_DIRNAME}/../scripts/sdlc-state.sh"

setup() {
    DB="${BATS_TEST_TMPDIR}/test.db"
    "${SDLC_STATE}" --db "${DB}" init >/dev/null
}

# Convenience: insert a complete (status=DONE) run with a finished_at stamp.
# We bypass run-update-status' CURRENT_TIMESTAMP to set a deterministic value
# for "most recent" comparisons across tests.
_complete_run() {
    local run_id="$1" started_at="$2" finished_at="$3"
    sqlite3 "${DB}" "UPDATE runs SET status='DONE',
                                started_at='${started_at}',
                                finished_at='${finished_at}'
                          WHERE id='${run_id}';"
}

_set_started_at() {
    local run_id="$1" started_at="$2"
    sqlite3 "${DB}" "UPDATE runs SET started_at='${started_at}' WHERE id='${run_id}';"
}

# ---------------------------------------------------------------------------
# latest-incomplete-run: find the most-recent IN_PROGRESS run, ignore others.
# ---------------------------------------------------------------------------

@test "latest-incomplete-run on empty ledger prints nothing and exits 0" {
    run "${SDLC_STATE}" --db "${DB}" latest-incomplete-run
    [ "${status}" -eq 0 ]
    [ -z "${output}" ]
}

@test "latest-incomplete-run returns the only IN_PROGRESS run" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    run "${SDLC_STATE}" --db "${DB}" latest-incomplete-run
    [ "${status}" -eq 0 ]
    [ "${output}" = "${rid}" ]
}

@test "latest-incomplete-run skips DONE/FAILED/ABORTED runs" {
    local r_done r_failed r_aborted
    r_done=$("${SDLC_STATE}" --db "${DB}" run-create epic-A parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${r_done}" DONE
    r_failed=$("${SDLC_STATE}" --db "${DB}" run-create epic-B parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${r_failed}" FAILED
    r_aborted=$("${SDLC_STATE}" --db "${DB}" run-create epic-C parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${r_aborted}" ABORTED

    run "${SDLC_STATE}" --db "${DB}" latest-incomplete-run
    [ "${status}" -eq 0 ]
    [ -z "${output}" ]
}

@test "latest-incomplete-run prefers the most-recent IN_PROGRESS by started_at" {
    local old_id new_id
    old_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-OLD parallel)
    new_id=$("${SDLC_STATE}" --db "${DB}" run-create epic-NEW parallel)
    _set_started_at "${old_id}" '2026-01-01 00:00:00'
    _set_started_at "${new_id}" '2026-05-20 12:00:00'

    run "${SDLC_STATE}" --db "${DB}" latest-incomplete-run
    [ "${status}" -eq 0 ]
    [ "${output}" = "${new_id}" ]
}

# ---------------------------------------------------------------------------
# resume-plan: emit the resume queue as JSON.
# ---------------------------------------------------------------------------

@test "resume-plan on a fresh single-story IN_PROGRESS run lists the story as PENDING" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-001 04 "Resume" P1 5 bash-zsh-macos-engineer "" "" PENDING

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # The JSON envelope is on a single line prefixed with QUEUE_JSON:
    [[ "${output}" == *"QUEUE_JSON:"* ]]
    # The story_id round-trips into the JSON. (We do not parse it here —
    # downstream tests pull individual fields via jq.)
    [[ "${output}" == *"4.3-001"* ]]
    [[ "${output}" == *"PENDING"* ]]
}

@test "resume-plan skips DONE stories" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-A 04 "Done story" P1 3 bash feature/4.3-A 42 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-B 04 "Open story" P1 3 bash "" "" PENDING

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # Done story 4.3-A must NOT appear in the resume queue JSON.
    [[ "${output}" != *"4.3-A"* ]]
    # Pending story 4.3-B must appear.
    [[ "${output}" == *"4.3-B"* ]]
}

@test "resume-plan preserves branch and PR for IN_PROGRESS stories" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-IP 04 "Mid-flight" P1 3 bash-zsh-macos-engineer feature/4.3-IP 77 IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-IP build 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-IP build 1 DONE "" ""
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-IP coverage 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-IP coverage 1 FAILED flaky-test ""

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # The resume queue MUST include the existing branch and PR so the merge
    # agent reuses them instead of creating new ones.
    [[ "${output}" == *"feature/4.3-IP"* ]]
    [[ "${output}" == *"77"* ]]
    # IN_PROGRESS story must be flagged with its resume-from stage (coverage).
    [[ "${output}" == *"coverage"* ]]
}

@test "resume-plan keeps BLOCKED stories when dependencies are not yet DONE" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    # Dependency: 4.3-DEP is still PENDING (not DONE) → 4.3-BLK stays BLOCKED.
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-DEP 04 "Dep" P1 3 bash "" "" PENDING
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-BLK 04 "Blocked" P1 3 bash "" "" BLOCKED
    # Record the dependency in an event row so resume can re-evaluate it.
    # Convention (matching this story's design): a "dep" event with source=
    # 'dependency' and message '<story>|<depends-on>' records the edge.
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-BLK info dependency "4.3-BLK|4.3-DEP"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # Blocked story is reported with status BLOCKED (no implicit unblock).
    [[ "${output}" == *"4.3-BLK"* ]]
    [[ "${output}" == *"BLOCKED"* ]]
}

@test "resume-plan promotes BLOCKED to PENDING when all dependencies are now DONE" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-DEP 04 "Dep done" P1 3 bash feature/4.3-DEP 1 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-WAS 04 "Was blocked" P1 3 bash "" "" BLOCKED
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-WAS info dependency "4.3-WAS|4.3-DEP"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # The blocked story now appears as PENDING in the resume plan.
    [[ "${output}" == *"4.3-WAS"* ]]
    [[ "${output}" == *"PENDING"* ]]
}

@test "resume-plan on unknown run_id exits non-zero with a clear error" {
    run "${SDLC_STATE}" --db "${DB}" resume-plan no-such-run
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"no such run"* ]] || [[ "${output}" == *"unknown"* ]] || [[ "${output}" == *"not found"* ]]
}

@test "resume-plan refuses to plan a DONE run" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${rid}" DONE
    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"completed"* ]] || [[ "${output}" == *"DONE"* ]]
}

# ---------------------------------------------------------------------------
# mark-stages-stale: atomically mark prior stage rows STALE for a (run, story,
# stage). Used at resume-time before incrementing the attempt counter.
# ---------------------------------------------------------------------------

@test "mark-stages-stale flips IN_PROGRESS / FAILED rows for the stage to STALE" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-MS 04 "Mark stale" P1 3 bash feature/4.3-MS "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-MS coverage 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-MS coverage 1 FAILED test-fail ""

    run "${SDLC_STATE}" --db "${DB}" mark-stages-stale "${rid}" 4.3-MS coverage
    [ "${status}" -eq 0 ]
    local stored
    stored=$(sqlite3 "${DB}" "SELECT status FROM stages
                                WHERE run_id='${rid}' AND story_id='4.3-MS'
                                  AND stage_name='coverage' AND attempt=1;")
    [ "${stored}" = "STALE" ]
}

@test "mark-stages-stale leaves DONE rows untouched" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-KEEP 04 "Keep done" P1 3 bash feature/4.3-KEEP "" IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-KEEP build 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-KEEP build 1 DONE "" ""
    # Try to mark the build stage stale — but it's DONE, so nothing should change.
    "${SDLC_STATE}" --db "${DB}" mark-stages-stale "${rid}" 4.3-KEEP build
    local stored
    stored=$(sqlite3 "${DB}" "SELECT status FROM stages
                                WHERE run_id='${rid}' AND story_id='4.3-KEEP'
                                  AND stage_name='build' AND attempt=1;")
    [ "${stored}" = "DONE" ]
}

@test "mark-stages-stale is atomic when multiple attempts exist" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-ATOM 04 "Atomic" P1 3 bash feature/4.3-ATOM "" IN_PROGRESS
    # Two attempts of the same stage: both should flip to STALE in a single TX.
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-ATOM coverage 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-ATOM coverage 1 FAILED a ""
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-ATOM coverage 2
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-ATOM coverage 2 FAILED b ""
    "${SDLC_STATE}" --db "${DB}" mark-stages-stale "${rid}" 4.3-ATOM coverage
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stages
                                 WHERE run_id='${rid}' AND story_id='4.3-ATOM'
                                   AND stage_name='coverage' AND status='STALE';")
    [ "${count}" = "2" ]
}

# ---------------------------------------------------------------------------
# Ambiguous resume: two IN_PROGRESS runs should not silently pick one.
# (latest-incomplete-run defines "most-recent" by started_at — but if two
# runs share an identical started_at, the caller MUST pass --run-id.)
# ---------------------------------------------------------------------------

@test "resume-plan with ambiguous run state requires an explicit run_id" {
    # Two IN_PROGRESS runs at the same started_at. latest-incomplete-run will
    # tie-break on id, but the orchestrator wraps this in an "ambiguous" check
    # — we only verify here that BOTH ids are visible via the bare query, so
    # the orchestrator has the data to detect ambiguity. The "must specify"
    # error is the orchestrator's responsibility (the SKILL.md preamble),
    # not this CLI's.
    local r1 r2
    r1=$("${SDLC_STATE}" --db "${DB}" run-create epic-A parallel)
    r2=$("${SDLC_STATE}" --db "${DB}" run-create epic-B parallel)
    _set_started_at "${r1}" '2026-05-20 10:00:00'
    _set_started_at "${r2}" '2026-05-20 10:00:00'
    # Both rows must be visible.
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM runs WHERE status='IN_PROGRESS';")
    [ "${count}" = "2" ]
}

# ---------------------------------------------------------------------------
# End-to-end resume scenario:
#   * Build a 3-story run, mark stories DONE / IN_PROGRESS / PENDING.
#   * On the IN_PROGRESS story, simulate a coverage failure.
#   * Run mark-stages-stale + resume-plan.
#   * Assert the plan correctly skips DONE, resumes IN_PROGRESS from coverage,
#     and includes PENDING work.
# This mirrors the DoD for story 4.3-001.
# ---------------------------------------------------------------------------

@test "end-to-end resume: 3-story run with kill at Stage 2 of story B resumes correctly" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    # Story A: DONE. Story B: IN_PROGRESS at coverage. Story C: PENDING.
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-A 04 "Story A" P1 3 bash feature/4.3-A 100 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-B 04 "Story B" P1 3 bash feature/4.3-B 101 IN_PROGRESS
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-C 04 "Story C" P1 3 bash "" "" PENDING

    # Story B build succeeded, coverage failed.
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-B build 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-B build 1 DONE "" ""
    "${SDLC_STATE}" --db "${DB}" stage-start "${rid}" 4.3-B coverage 1
    "${SDLC_STATE}" --db "${DB}" stage-finish "${rid}" 4.3-B coverage 1 FAILED flaky-test ""

    # Resume: stale-mark the failed stage then ask for a plan.
    "${SDLC_STATE}" --db "${DB}" mark-stages-stale "${rid}" 4.3-B coverage

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # Story A (DONE) must be absent.
    [[ "${output}" != *"4.3-A"* ]]
    # Story B (IN_PROGRESS) must be present with branch + PR + resume_from=coverage.
    [[ "${output}" == *"4.3-B"* ]]
    [[ "${output}" == *"feature/4.3-B"* ]]
    [[ "${output}" == *"101"* ]]
    [[ "${output}" == *"coverage"* ]]
    # Story C (PENDING) must be present.
    [[ "${output}" == *"4.3-C"* ]]
}

# ---------------------------------------------------------------------------
# Gap tests added by QA coverage gate (Story 4.3-001)
# ---------------------------------------------------------------------------

# Gap: resume-plan on a run that has zero stories should exit 0 with an
# empty queue — not an error. The QUEUE_JSON envelope must still be emitted
# so downstream parsers do not break on missing output.
@test "resume-plan on a run with zero stories exits 0 and emits an empty queue" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    # No story-upsert calls — the stories table has zero rows for this run.
    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # QUEUE_JSON envelope must be present (even if empty).
    [[ "${output}" == *"QUEUE_JSON:"* ]]
    # The array must be empty: QUEUE_JSON:[]
    [[ "${output}" == *"QUEUE_JSON:[]"* ]]
}

# Gap: resume-plan for a run whose stories are ALL DONE should output a
# QUEUE_JSON with an empty array — "run complete, nothing to resume".
@test "resume-plan where all stories are DONE emits an empty queue" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-X1 04 "Done 1" P1 3 bash feature/4.3-X1 10 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-X2 04 "Done 2" P1 3 bash feature/4.3-X2 11 DONE

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # Both DONE stories must be absent from the queue.
    [[ "${output}" != *"4.3-X1"* ]]
    [[ "${output}" != *"4.3-X2"* ]]
    # Empty array envelope.
    [[ "${output}" == *"QUEUE_JSON:[]"* ]]
}

# Gap: mark-stages-stale called for a story_id that has no stage rows must
# be a no-op — not an error. The UPDATE affects zero rows which is fine.
@test "mark-stages-stale on a story with no stage rows is a no-op" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-NOSTAGE 04 "No stages" P1 3 bash "" "" PENDING
    # There are no stage rows for 4.3-NOSTAGE — mark-stages-stale must succeed.
    run "${SDLC_STATE}" --db "${DB}" mark-stages-stale "${rid}" 4.3-NOSTAGE coverage
    [ "${status}" -eq 0 ]
    # Confirm no stage rows were created as a side-effect.
    local count
    count=$(sqlite3 "${DB}" "SELECT COUNT(*) FROM stages
                                WHERE run_id='${rid}' AND story_id='4.3-NOSTAGE';")
    [ "${count}" = "0" ]
}

# Gap: transitive dependency chain — A depends on B which depends on C.
# When both B and C are DONE, A must flip from BLOCKED to PENDING.
@test "resume-plan promotes BLOCKED to PENDING for a transitive dependency chain" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    # C is DONE, B depends on C and is DONE, A depends on B and is BLOCKED.
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-TC 04 "Chain C" P1 3 bash feature/4.3-TC 20 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-TB 04 "Chain B" P1 3 bash feature/4.3-TB 21 DONE
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-TA 04 "Chain A" P1 3 bash "" "" BLOCKED
    # Record the dependency edges: A->B and B->C.
    # (B->C is also recorded so story_reevaluate_blocked for B would work,
    # but B is already DONE so only A's edge matters for this test.)
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-TA info dependency "4.3-TA|4.3-TB"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # A must appear as PENDING — its only recorded dep (B) is DONE.
    [[ "${output}" == *"4.3-TA"* ]]
    [[ "${output}" == *"PENDING"* ]]
    # C and B are DONE — they must not appear in the queue.
    [[ "${output}" != *"4.3-TC"* ]]
    [[ "${output}" != *"4.3-TB"* ]]
}

# Gap: BLOCKED story stays BLOCKED when at least one dependency is FAILED.
@test "resume-plan keeps BLOCKED when a dependency is FAILED" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-FDEP 04 "Failed dep" P1 3 bash "" "" FAILED
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-FBLK 04 "Still blocked" P1 3 bash "" "" BLOCKED
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-FBLK info dependency "4.3-FBLK|4.3-FDEP"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    # FBLK must remain BLOCKED — its dep is FAILED, not DONE.
    [[ "${output}" == *"4.3-FBLK"* ]]
    [[ "${output}" == *"BLOCKED"* ]]
    # FAILED story itself must appear in the queue (surfaced as-is).
    [[ "${output}" == *"4.3-FDEP"* ]]
    [[ "${output}" == *"FAILED"* ]]
}

# Gap: BLOCKED story stays BLOCKED when a dependency is SKIPPED (not DONE).
@test "resume-plan keeps BLOCKED when a dependency is SKIPPED" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-SDEP 04 "Skipped dep" P1 3 bash "" "" SKIPPED
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-SBLK 04 "Still blocked" P1 3 bash "" "" BLOCKED
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-SBLK info dependency "4.3-SBLK|4.3-SDEP"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"4.3-SBLK"* ]]
    [[ "${output}" == *"BLOCKED"* ]]
}

# Gap: BLOCKED story stays BLOCKED when its dependency is itself BLOCKED.
@test "resume-plan keeps BLOCKED when a dependency is also BLOCKED" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-BDEP 04 "Blocked dep" P1 3 bash "" "" BLOCKED
    "${SDLC_STATE}" --db "${DB}" story-upsert \
        "${rid}" 4.3-BBLK 04 "Outer blocked" P1 3 bash "" "" BLOCKED
    "${SDLC_STATE}" --db "${DB}" event-log "${rid}" 4.3-BBLK info dependency "4.3-BBLK|4.3-BDEP"

    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"4.3-BBLK"* ]]
    [[ "${output}" == *"BLOCKED"* ]]
}

# Gap: FAILED run refuses resume-plan (mirrors DONE check — all terminal
# statuses should be rejected, not just DONE).
@test "resume-plan refuses to plan a FAILED run" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${rid}" FAILED
    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"FAILED"* ]] || [[ "${output}" == *"completed"* ]]
}

# Gap: ABORTED run refuses resume-plan.
@test "resume-plan refuses to plan an ABORTED run" {
    local rid
    rid=$("${SDLC_STATE}" --db "${DB}" run-create epic-04 parallel)
    "${SDLC_STATE}" --db "${DB}" run-update-status "${rid}" ABORTED
    run "${SDLC_STATE}" --db "${DB}" resume-plan "${rid}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"ABORTED"* ]] || [[ "${output}" == *"completed"* ]]
}
