#!/usr/bin/env bats
# ABOUTME: Coverage for Story 27.0-001 — docs/optimization/BASELINE.md baseline doc and its reproduction code.
#
# This is a docs-only story, but its Definition of Done requires that the
# reproduction scripts "run cleanly". These tests anchor the baseline doc in
# the build by verifying:
#   - the doc exists with the three measured tables (per-stage cost, model
#     mix, healthy-vs-outlier durations) and the epic exit-criteria section
#   - both embedded Python reproduction scripts (§4.1 stage-log aggregation,
#     §4.2 transcript model-mix scan) parse and produce correct tables when
#     run against synthetic fixtures
#   - both §4.3 ledger SQL queries run against a fixture .sdlc-state.db

REPO_ROOT="${BATS_TEST_DIRNAME}/.."
DOC="$REPO_ROOT/docs/optimization/BASELINE.md"

# Extract the Nth fenced code block of a given language from the doc.
# Usage: extract_block <lang> <index-from-1>
extract_block() {
    awk -v lang="$1" -v want="$2" '
        $0 == "```" lang { n++; if (n == want) { inb = 1; next } }
        inb && $0 == "```" { exit }
        inb { print }
    ' "$DOC"
}

setup() {
    TMP="$BATS_TEST_TMPDIR"
}

# --- Doc structure ---------------------------------------------------------

@test "BASELINE.md exists, is non-empty, and starts with an ABOUTME comment" {
    [ -f "$DOC" ]
    [ -s "$DOC" ]
    head -n 1 "$DOC" | grep -qF "ABOUTME:"
}

@test "BASELINE.md contains the per-stage cost/token/duration table (§1)" {
    grep -qE "^#+ .*Controller path" "$DOC"
    grep -qF "| Stage | Dispatches | Total cost | Avg cost |" "$DOC"
    # The two dominant stages from the 50-story aggregation must be present.
    grep -qE '^\| build \| 50 \|' "$DOC"
    grep -qE '^\| coverage \| 47 \|' "$DOC"
}

@test "BASELINE.md contains the interactive model-mix table (§2) with the Opus share" {
    grep -qF "| Model | Messages | Output tok | Cache-read tok | Output share |" "$DOC"
    grep -qF "claude-opus-4-8" "$DOC"
    # The headline finding: ~94% of interactive output tokens run on Opus.
    grep -qF "93.3%" "$DOC"
}

@test "BASELINE.md contains the healthy-vs-outlier stage-duration table (§3)" {
    grep -qF "| Stage | n | p50 (min) | p90 (min) | max (min) | stages > 1h |" "$DOC"
    # The worst outlier run called out in the analysis must be identified.
    grep -qF "cdfb8cc8-f2be-422f-92d3-23331c5180cc" "$DOC"
}

@test "BASELINE.md states the Epic-27 exit measurements (§5)" {
    grep -qE "^#+ .*success criteria" "$DOC"
    grep -qF "| Metric | Baseline (this doc) | Exit target | Measured by |" "$DOC"
    grep -qF "Opus share of interactive output tokens" "$DOC"
    grep -qF "Controller cost per story" "$DOC"
}

@test "BASELINE.md reproduction section carries both scripts and the ledger SQL" {
    grep -qF "stage_usage.py" "$DOC"
    grep -qF "model_mix.py" "$DOC"
    grep -qF "sqlite3 -header -column .sdlc-state.db" "$DOC"
}

# --- §4.1 stage-log aggregation script -------------------------------------

@test "stage_usage.py (§4.1) compiles" {
    extract_block python 1 > "$TMP/stage_usage.py"
    [ -s "$TMP/stage_usage.py" ]
    python3 -m py_compile "$TMP/stage_usage.py"
}

@test "stage_usage.py aggregates a fixture stage log into the stage table" {
    extract_block python 1 > "$TMP/stage_usage.py"
    mkdir -p "$TMP/logs/run1"
    printf '%s\n' \
        '{"type":"system","subtype":"init"}' \
        '{"type":"result","usage":{"output_tokens":100,"cache_read_input_tokens":2000,"cache_creation_input_tokens":50},"total_cost_usd":1.25,"duration_ms":60000}' \
        > "$TMP/logs/run1/27.0-001-build-1.log"
    # A log without a result line must be counted as skipped, not aggregated.
    printf '%s\n' '{"type":"system","subtype":"init"}' \
        > "$TMP/logs/run1/27.0-001-review-1.log"

    run python3 "$TMP/stage_usage.py" "$TMP/logs"
    [ "$status" -eq 0 ]
    [[ "$output" == *"stories with usage logs: 1"* ]]
    [[ "$output" == *"stage logs skipped (no result line / unparsable name): 1"* ]]
    [[ "$output" == *"total cost: \$1.25   avg per story: \$1.25"* ]]
    [[ "$output" == *"| build | 1 | \$1.25 | \$1.25 | 100 | 2,000 | 1.0 | 100% |"* ]]
}

# --- §4.2 transcript model-mix scan ----------------------------------------

@test "model_mix.py (§4.2) compiles" {
    extract_block python 2 > "$TMP/model_mix.py"
    [ -s "$TMP/model_mix.py" ]
    python3 -m py_compile "$TMP/model_mix.py"
}

@test "model_mix.py tallies fixture transcripts and excludes worktree project dirs" {
    extract_block python 2 > "$TMP/model_mix.py"
    local projects="$TMP/home/.claude/projects"
    mkdir -p "$projects/-tmp-claude-code-config" \
             "$projects/-tmp-claude-code-config--claude-worktrees-x"
    printf '%s\n' \
        '{"type":"assistant","timestamp":"2026-07-01T00:00:00Z","message":{"model":"claude-opus-4-8","usage":{"output_tokens":500,"cache_read_input_tokens":1000,"cache_creation_input_tokens":10}}}' \
        > "$projects/-tmp-claude-code-config/session.jsonl"
    # Controller worktree traffic must not leak into the interactive tally.
    printf '%s\n' \
        '{"type":"assistant","timestamp":"2026-07-01T00:00:00Z","message":{"model":"claude-sonnet-5","usage":{"output_tokens":999}}}' \
        > "$projects/-tmp-claude-code-config--claude-worktrees-x/session.jsonl"

    HOME="$TMP/home" run python3 "$TMP/model_mix.py"
    [ "$status" -eq 0 ]
    [[ "$output" == *"sessions with usage in window: 1"* ]]
    [[ "$output" == *"total output tokens: 500"* ]]
    [[ "$output" == *"| claude-opus-4-8 | 1 | 500 | 1,000 | 100.0% |"* ]]
    [[ "$output" != *"claude-sonnet-5"* ]]
}

@test "model_mix.py honours the since-date window argument" {
    extract_block python 2 > "$TMP/model_mix.py"
    local projects="$TMP/home/.claude/projects"
    mkdir -p "$projects/-tmp-claude-code-config"
    printf '%s\n' \
        '{"type":"assistant","timestamp":"2026-05-01T00:00:00Z","message":{"model":"claude-opus-4-8","usage":{"output_tokens":500}}}' \
        > "$projects/-tmp-claude-code-config/session.jsonl"

    # Message predates the window start, so nothing should be tallied.
    HOME="$TMP/home" run python3 "$TMP/model_mix.py" 2026-06-10
    [ "$status" -eq 0 ]
    [[ "$output" == *"total output tokens: 0"* ]]
}

# --- §4.3 ledger duration SQL ----------------------------------------------

make_fixture_db() {
    sqlite3 "$1" <<'SQL'
CREATE TABLE stages (
    run_id TEXT,
    stage_name TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT
);
INSERT INTO stages VALUES
    ('run-a', 'build',    'DONE', '2026-06-15 10:00:00', '2026-06-15 10:10:00'),
    ('run-a', 'coverage', 'DONE', '2026-06-15 10:10:00', '2026-06-15 10:14:00'),
    ('run-b', 'coverage', 'DONE', '2026-06-16 09:00:00', '2026-06-16 17:24:00'),
    ('run-a', 'build',    'FAILED', '2026-06-17 10:00:00', '2026-06-17 10:30:00'),
    ('run-a', 'build',    'DONE', '2026-05-01 10:00:00', '2026-05-01 10:05:00');
SQL
}

@test "ledger duration SQL (§4.3) runs against a fixture .sdlc-state.db" {
    extract_block sh 1 > "$TMP/durations.sh"
    [ -s "$TMP/durations.sh" ]
    make_fixture_db "$TMP/.sdlc-state.db"

    cd "$TMP"
    run bash durations.sh
    [ "$status" -eq 0 ]
    # One DONE build inside the window (10 min); FAILED and out-of-window rows excluded.
    [[ "$output" == *"build"* ]]
    [[ "$output" == *"10.0"* ]]
    # run-b's 504-minute coverage stage must count as an over-1h outlier.
    [[ "$output" == *"coverage"* ]]
    [[ "$output" == *"504.0"* ]]
}

@test "outlier drill-down SQL (§4.3) ranks the worst coverage run first" {
    extract_block sh 2 > "$TMP/drilldown.sh"
    [ -s "$TMP/drilldown.sh" ]
    make_fixture_db "$TMP/.sdlc-state.db"

    cd "$TMP"
    run bash drilldown.sh
    [ "$status" -eq 0 ]
    [[ "${lines[2]}" == run-b* ]]
    [[ "$output" == *"504"* ]]
}
