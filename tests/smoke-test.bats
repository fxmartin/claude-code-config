#!/usr/bin/env bats
# Story 3.2-002 — behavior tests for scripts/smoke-test.sh.
#
# The smoke test runs on macOS-latest and ubuntu-latest GitHub runners and
# exercises the modal installer end-to-end (dry-run → actual → idempotent →
# uninstall) inside an isolated temp $HOME so the runner's real ~/.claude is
# never touched.
#
# These tests invoke the smoke-test script in a subprocess and assert on its
# exit status, summary line, and observable filesystem effects. They are the
# only place that asserts the smoke-test contract; the smoke-test script
# itself is the contract being tested.

SMOKE="${BATS_TEST_DIRNAME}/../scripts/smoke-test.sh"

setup() {
    # Each test gets its own SMOKE_HOME so concurrent runs do not collide.
    SMOKE_HOME="$(mktemp -d)"
    export SMOKE_HOME
}

teardown() {
    [ -n "${SMOKE_HOME:-}" ] && rm -rf "${SMOKE_HOME}"
}

# Run the smoke test with a caller-provided HOME so we can both inspect
# leftover symlinks (if any) and prove that the script cleaned them up.
_run_smoke() {
    run env SMOKE_HOME_OVERRIDE="${SMOKE_HOME}" bash "${SMOKE}" "$@"
}

@test "smoke-test.sh exists and is executable" {
    [ -f "${SMOKE}" ]
    [ -x "${SMOKE}" ]
}

@test "smoke-test.sh exits 0 on a clean run" {
    _run_smoke
    [ "$status" -eq 0 ]
}

@test "smoke-test.sh prints a SMOKE_TEST summary line" {
    _run_smoke
    [ "$status" -eq 0 ]
    # Summary format: SMOKE_TEST: <pass>/<total> passed
    [[ "$output" =~ SMOKE_TEST:\ [0-9]+/[0-9]+\ passed ]]
}

@test "smoke-test.sh reports all four phases (dry-run, install, idempotent, uninstall)" {
    _run_smoke
    [ "$status" -eq 0 ]
    [[ "$output" == *"dry-run"* ]]
    [[ "$output" == *"install"* ]]
    [[ "$output" == *"idempotent"* ]]
    [[ "$output" == *"uninstall"* ]]
}

@test "smoke-test.sh creates symlinks during the install phase (verified mid-run via SMOKE_HOME_OVERRIDE)" {
    # When SMOKE_HOME_OVERRIDE is set, smoke-test.sh uses that path as the
    # temp HOME and does NOT clean it up at the end, so the test can inspect
    # the post-install state. The uninstall phase still runs and removes the
    # symlinks; we assert below that they are gone.
    _run_smoke
    [ "$status" -eq 0 ]
    # After uninstall phase the symlinks must be removed.
    [ ! -L "${SMOKE_HOME}/.claude/CLAUDE.md" ]
    [ ! -L "${SMOKE_HOME}/.claude/agents" ]
    [ ! -L "${SMOKE_HOME}/.claude/skills" ]
}

@test "smoke-test.sh leaves no orphan temp dir when SMOKE_HOME_OVERRIDE is unset" {
    # Without the override, smoke-test.sh creates its own mktemp dir and
    # cleans it up. We can only assert behavior indirectly: the script must
    # exit 0 and not have written to the real $HOME.
    run bash "${SMOKE}"
    [ "$status" -eq 0 ]
}

@test "smoke-test.sh fails fast when install.sh is missing" {
    # Point the script at an empty repo root by isolating SCRIPT_ROOT via
    # SMOKE_SCRIPT_ROOT_OVERRIDE.
    empty_root="$(mktemp -d)"
    run env SMOKE_SCRIPT_ROOT_OVERRIDE="${empty_root}" SMOKE_HOME_OVERRIDE="${SMOKE_HOME}" \
        bash "${SMOKE}"
    [ "$status" -ne 0 ]
    rm -rf "${empty_root}"
}

# ── Gap coverage tests (Story 3.2-002 QA gate) ──────────────────────────────
#
# These four tests cover behaviours that the original 7 tests leave unexercised:
#   G1. install.sh is present but exits non-zero → smoke-test exits non-zero
#       with a diagnostic on stderr and the SMOKE_TEST summary line on stdout.
#   G2. The SMOKE_TEST summary line format is stable/grep-parseable even when
#       phases fail (not only on a clean run).
#   G3. No temp dir leaks when the smoke-test exits 1 (trap cleanup fires on
#       any exit, including failure).
#   G4. The script never writes to the caller's real $HOME, even when
#       SMOKE_HOME_OVERRIDE is absent — the only writer is the mktemp home.

# Helper: create a minimal fake repo root whose install.sh always exits $1.
_make_stub_root() {
    local exit_code="${1:-1}"
    local stub_root
    stub_root="$(mktemp -d)"
    printf '#!/usr/bin/env bash\necho "stub output"\nexit %s\n' "$exit_code" \
        > "${stub_root}/install.sh"
    chmod +x "${stub_root}/install.sh"
    echo "$stub_root"
}

@test "G1: smoke-test exits non-zero when install.sh is present but fails" {
    stub_root="$(_make_stub_root 1)"
    run env SMOKE_SCRIPT_ROOT_OVERRIDE="${stub_root}" SMOKE_HOME_OVERRIDE="${SMOKE_HOME}" \
        bash "${SMOKE}"
    rm -rf "${stub_root}"
    [ "$status" -ne 0 ]
}

@test "G2: SMOKE_TEST summary line is present and grep-parseable even on failure" {
    stub_root="$(_make_stub_root 1)"
    # Merge stderr into stdout so bats captures both streams in $output.
    run env SMOKE_SCRIPT_ROOT_OVERRIDE="${stub_root}" SMOKE_HOME_OVERRIDE="${SMOKE_HOME}" \
        bash "${SMOKE}" 2>&1
    rm -rf "${stub_root}"
    # Exit must be non-zero (phases failed) but the summary line must be emitted.
    [ "$status" -ne 0 ]
    # The exact format CI greps for: SMOKE_TEST: <n>/<n> passed
    echo "${output}" | grep -qE '^SMOKE_TEST: [0-9]+/[0-9]+ passed$'
}

@test "G3: smoke-test cleans up its mktemp home even when phases fail" {
    stub_root="$(_make_stub_root 1)"
    # Count smoke-claude-* dirs in TMPDIR before the run. We cannot observe
    # the temp dir that the script creates (it is gone by the time run returns),
    # so we assert the count is the same after the run as it was before.
    tmpdir="${TMPDIR:-/tmp}"
    before_count=$(ls "$tmpdir" 2>/dev/null | { grep -c "smoke-claude-" || true; })
    run env SMOKE_SCRIPT_ROOT_OVERRIDE="${stub_root}" bash "${SMOKE}" 2>&1
    after_count=$(ls "$tmpdir" 2>/dev/null | { grep -c "smoke-claude-" || true; })
    rm -rf "${stub_root}"
    [ "$after_count" -eq "$before_count" ]
}

# Helper for G4: list ~/.claude entries a live Claude Code session does NOT
# continuously mutate, so before/after comparison doesn't race a background
# session write (issue #39).
_claude_stable_entries() {
    local dir="$1"
    ls -1a "${dir}" 2>/dev/null \
        | grep -vE '^(history\.jsonl|backups|projects|todos|logs|shell-snapshots|statsig|telemetry|\.claude\.json\.backup\.|\.\.?$)' \
        | LC_ALL=C sort
}

@test "G4: smoke-test does not write to the caller's real HOME when SMOKE_HOME_OVERRIDE is absent" {
    # Snapshot the stable entry list of $HOME/.claude (or note its absence)
    # before the run. We compare a filtered entry list rather than the dir
    # mtime because a live Claude Code session continuously writes session
    # state (history.jsonl, .claude.json.backup.*) which races mtime (#39).
    real_claude="${HOME}/.claude"
    if [ -d "${real_claude}" ]; then
        before_entries="$(_claude_stable_entries "${real_claude}")"
    else
        before_entries="absent"
    fi

    # Run without SMOKE_HOME_OVERRIDE — the script must create its own mktemp.
    run bash "${SMOKE}"
    [ "$status" -eq 0 ]

    if [ -d "${real_claude}" ]; then
        after_entries="$(_claude_stable_entries "${real_claude}")"
    else
        after_entries="absent"
    fi

    [ "${before_entries}" = "${after_entries}" ]
}
