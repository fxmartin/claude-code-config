#!/usr/bin/env bats
# Story 29.2-002 — scripts/opencode-worktree-smoke.sh proves (or disproves)
# that OpenCode's build adapter can complete a small edit task unattended
# inside a freshly cut git worktree, and that two such worktrees running
# concurrently stay isolated. It shares its scratch-repo / concurrent-worktree
# logic with scripts/qwen-worktree-smoke.sh via scripts/harness-worktree-smoke.sh
# (tests/harness-worktree-smoke.bats covers that shared script directly).
#
# The script only ever operates on a throwaway scratch repo under a mktemp
# sandbox (mirrors controller/tests/conftest.py's hermeticity guards) — it
# never touches this real repo. These tests stub `opencode` on PATH so the
# suite never depends on real auth/network/model reachability.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    SCRIPT="$REPO_ROOT/scripts/opencode-worktree-smoke.sh"
    SANDBOX="$(mktemp -d)"
    TEST_BIN="$SANDBOX/bin"
    mkdir -p "$TEST_BIN"
    export PATH="$TEST_BIN:$PATH"
    export OPENCODE_ARG_LOG="$SANDBOX/opencode-args.log"
}

teardown() {
    [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

_install_succeeding_opencode() {
    # Mirrors real OpenCode 1.18.15: `--version` is a pure query with no
    # filesystem side effects; only `run` writes the edit. See
    # tests/qwen-worktree-smoke.bats for why the version probe must stay
    # side-effect-free (an earlier stub leaked a file into the real repo).
    cat > "$TEST_BIN/opencode" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "0.0.0-fake"
    exit 0
fi
if [[ "$1" == "run" ]]; then
    shift
    cat >/dev/null
    printf '%s\n' "$@" >> "${OPENCODE_ARG_LOG}"
    echo done > "edited-by-opencode.txt"
    cat <<'RESULT'
opencode reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"n/a","build_status":"SUCCESS","commit_sha":"n/a"}
<<<END_RESULT>>>
RESULT
    exit 0
fi
echo "unexpected invocation: $*" >&2
exit 1
EOF
    chmod +x "$TEST_BIN/opencode"
}

_install_unreachable_model_opencode() {
    # Mirrors the real failure mode documented in harnesses.yaml: an
    # unreachable default provider. Here it fails fast instead of hanging so
    # the bats suite stays fast; the isolation guarantees must hold either way.
    cat > "$TEST_BIN/opencode" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "0.0.0-fake"
    exit 0
fi
echo "provider error: could not reach model" >&2
exit 1
EOF
    chmod +x "$TEST_BIN/opencode"
}

_install_mixed_opencode() {
    # wt-2 fails, every other worktree succeeds — proves a per-worktree
    # failure never gets mistaken for (or masked by) a sibling's success.
    cat > "$TEST_BIN/opencode" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "0.0.0-fake"
    exit 0
fi
if [[ "$(basename "$PWD")" == "wt-2" ]]; then
    echo "provider error: could not reach model" >&2
    exit 1
fi
shift
cat >/dev/null
echo done > "edited-by-opencode.txt"
cat <<'RESULT'
opencode reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"n/a","build_status":"SUCCESS","commit_sha":"n/a"}
<<<END_RESULT>>>
RESULT
EOF
    chmod +x "$TEST_BIN/opencode"
}

@test "single worktree: successful opencode run edits only that worktree" {
    _install_succeeding_opencode
    run bash "$SCRIPT" --worktrees 1 --sandbox "$SANDBOX/run1" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run1/wt-1/edited-by-opencode.txt" ]
    # The scratch repo root itself must stay untouched.
    [ ! -f "$SANDBOX/run1/scratch-repo/edited-by-opencode.txt" ]
    run git -C "$SANDBOX/run1/scratch-repo" status --porcelain
    [ -z "$output" ]
    grep -q '"overall": "PASS"' "$SANDBOX/evidence.json"
}

@test "the seeded opencode.json permission config is present in every worktree" {
    _install_succeeding_opencode
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run2" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run2/wt-1/opencode.json" ]
    [ -f "$SANDBOX/run2/wt-2/opencode.json" ]
    grep -q '"edit": "allow"' "$SANDBOX/run2/wt-1/opencode.json"
    grep -q '"bash": "allow"' "$SANDBOX/run2/wt-1/opencode.json"
}

@test "two concurrent worktrees stay isolated from each other and the repo root" {
    _install_succeeding_opencode
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run3" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run3/wt-1/edited-by-opencode.txt" ]
    [ -f "$SANDBOX/run3/wt-2/edited-by-opencode.txt" ]
    run git -C "$SANDBOX/run3/scratch-repo" status --porcelain
    [ -z "$output" ]
    grep -q '"isolation_ok": true' "$SANDBOX/evidence.json"
}

@test "evidence file records opencode version and flags used" {
    _install_succeeding_opencode
    OPENCODE_FLAGS="--model anthropic/claude-haiku-4-5" run bash "$SCRIPT" --worktrees 1 --sandbox "$SANDBOX/run4" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    grep -q '"flags"' "$SANDBOX/evidence.json"
    grep -q '"opencode_version"' "$SANDBOX/evidence.json"
}

@test "an unreachable-model failure is recorded as FAIL, not a crash, and leaks no partial write" {
    _install_unreachable_model_opencode
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run5" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 1 ]
    [ ! -f "$SANDBOX/run5/wt-1/edited-by-opencode.txt" ]
    [ ! -f "$SANDBOX/run5/wt-2/edited-by-opencode.txt" ]
    grep -q '"overall": "FAIL"' "$SANDBOX/evidence.json"
    run git -C "$SANDBOX/run5/scratch-repo" status --porcelain
    [ -z "$output" ]
}

@test "one worktree succeeding while its sibling fails keeps each edit confined to its own worktree" {
    _install_mixed_opencode
    run bash "$SCRIPT" --worktrees 2 --sandbox "$SANDBOX/run6" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 1 ]
    [ -f "$SANDBOX/run6/wt-1/edited-by-opencode.txt" ]
    [ ! -f "$SANDBOX/run6/wt-2/edited-by-opencode.txt" ]
    grep -q '"overall": "FAIL"' "$SANDBOX/evidence.json"
    # The successful sibling's file must never leak into the failed one.
    grep -q '"isolation_ok": true' "$SANDBOX/evidence.json"
}

@test "an unrecognised flag exits with a usage error instead of running opencode" {
    run bash "$SCRIPT" --bogus-flag
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument"* ]]
}
