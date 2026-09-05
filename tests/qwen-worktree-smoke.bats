#!/usr/bin/env bats
# Story 29.1-001 — scripts/qwen-worktree-smoke.sh proves (or disproves) that
# `qwen -p` can complete a small edit task unattended inside a freshly cut git
# worktree, and that two such worktrees running concurrently stay isolated.
#
# The script only ever operates on a throwaway scratch repo under a mktemp
# sandbox (mirrors controller/tests/conftest.py's hermeticity guards) — it
# never touches this real repo. These tests stub `qwen` on PATH so the suite
# never depends on real auth/network, and cover both the happy path and the
# real-world failure this story actually hit (qwen exits non-zero because no
# auth type is configured).

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    SCRIPT="$REPO_ROOT/scripts/qwen-worktree-smoke.sh"
    SANDBOX="$(mktemp -d)"
    TEST_BIN="$SANDBOX/bin"
    mkdir -p "$TEST_BIN"
    export PATH="$TEST_BIN:$PATH"
    export QWEN_ARG_LOG="$SANDBOX/qwen-args.log"
}

teardown() {
    [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

_install_succeeding_qwen() {
    # Mirrors real qwen: `--version` is a pure query with no filesystem side
    # effects; only an actual `-p` task invocation writes the edit. A stub that
    # wrote unconditionally previously leaked edited-by-qwen.txt into whatever
    # cwd the smoke script's top-level `qwen --version` evidence probe ran in
    # (the real repo root, since the probe intentionally runs unsandboxed).
    cat > "$TEST_BIN/qwen" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "0.0.0-fake"
    exit 0
fi
printf '%s\n' "$@" >> "${QWEN_ARG_LOG}"
echo done > "edited-by-qwen.txt"
cat <<'RESULT'
qwen reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"n/a","build_status":"SUCCESS","commit_sha":"n/a"}
<<<END_RESULT>>>
RESULT
EOF
    chmod +x "$TEST_BIN/qwen"
}

_install_unauthenticated_qwen() {
    cat > "$TEST_BIN/qwen" <<'EOF'
#!/usr/bin/env bash
echo "No auth type is selected. Please configure an auth type (e.g. via settings or --auth-type) before running in non-interactive mode." >&2
exit 1
EOF
    chmod +x "$TEST_BIN/qwen"
}

_install_mixed_qwen() {
    # wt-2 fails, every other worktree succeeds — proves a per-worktree
    # failure never gets mistaken for (or masked by) a sibling's success.
    cat > "$TEST_BIN/qwen" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "0.0.0-fake"
    exit 0
fi
if [[ "$(basename "$PWD")" == "wt-2" ]]; then
    echo "No auth type is selected." >&2
    exit 1
fi
printf '%s\n' "$@" >> "${QWEN_ARG_LOG}"
echo done > "edited-by-qwen.txt"
cat <<'RESULT'
qwen reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"n/a","build_status":"SUCCESS","commit_sha":"n/a"}
<<<END_RESULT>>>
RESULT
EOF
    chmod +x "$TEST_BIN/qwen"
}

@test "single worktree: successful qwen run edits only that worktree" {
    _install_succeeding_qwen
    run bash "$SCRIPT" --worktrees 1 --sandbox "$SANDBOX/run1" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run1/wt-1/edited-by-qwen.txt" ]
    # The scratch repo root itself must stay untouched.
    [ ! -f "$SANDBOX/run1/scratch-repo/edited-by-qwen.txt" ]
    run git -C "$SANDBOX/run1/scratch-repo" status --porcelain
    [ -z "$output" ]
    grep -q '"overall": "PASS"' "$SANDBOX/evidence.json"
}

@test "two concurrent worktrees stay isolated from each other and the repo root" {
    _install_succeeding_qwen
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run2" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run2/wt-1/edited-by-qwen.txt" ]
    [ -f "$SANDBOX/run2/wt-2/edited-by-qwen.txt" ]
    run git -C "$SANDBOX/run2/scratch-repo" status --porcelain
    [ -z "$output" ]
    grep -q '"isolation_ok": true' "$SANDBOX/evidence.json"
}

@test "evidence file records qwen version and flags used" {
    _install_succeeding_qwen
    QWEN_FLAGS="--yolo" run bash "$SCRIPT" --worktrees 1 --sandbox "$SANDBOX/run3" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    grep -q '"flags"' "$SANDBOX/evidence.json"
}

@test "an unattended-write failure (no auth configured) is recorded as verified-negative, not a crash" {
    _install_unauthenticated_qwen
    run bash "$SCRIPT" --worktrees 1 --sandbox "$SANDBOX/run4" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 1 ]
    [ ! -f "$SANDBOX/run4/wt-1/edited-by-qwen.txt" ]
    grep -q '"overall": "FAIL"' "$SANDBOX/evidence.json"
    grep -q "No auth type is selected" "$SANDBOX/evidence.json"
}

@test "failure in one worktree never leaks a partial write into the scratch repo root" {
    _install_unauthenticated_qwen
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run5" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 1 ]
    run git -C "$SANDBOX/run5/scratch-repo" status --porcelain
    [ -z "$output" ]
}

@test "one worktree succeeding while its sibling fails keeps each edit confined to its own worktree" {
    _install_mixed_qwen
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run6" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 1 ]
    [ -f "$SANDBOX/run6/wt-1/edited-by-qwen.txt" ]
    [ ! -f "$SANDBOX/run6/wt-2/edited-by-qwen.txt" ]
    grep -q '"overall": "FAIL"' "$SANDBOX/evidence.json"
    # The successful sibling's file must never leak into the failed one.
    grep -q '"isolation_ok": true' "$SANDBOX/evidence.json"
}

@test "an unrecognised flag exits with a usage error instead of running qwen" {
    run bash "$SCRIPT" --bogus-flag
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument"* ]]
}
