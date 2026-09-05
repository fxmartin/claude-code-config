#!/usr/bin/env bats
# Story 29.2-002 — scripts/harness-worktree-smoke.sh is the shared
# scratch-repo / concurrent-worktree isolation evidence script that both
# scripts/qwen-worktree-smoke.sh and scripts/opencode-worktree-smoke.sh wrap
# (see tests/qwen-worktree-smoke.bats and tests/opencode-worktree-smoke.bats
# for the per-harness scenario coverage). These tests exercise the shared
# script's own generic surface with a synthetic third harness, proving the
# parameterization itself (not any one adapter's behaviour) is what is under
# test here.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    SCRIPT="$REPO_ROOT/scripts/harness-worktree-smoke.sh"
    SANDBOX="$(mktemp -d)"
    TEST_BIN="$SANDBOX/bin"
    mkdir -p "$TEST_BIN"
    export PATH="$TEST_BIN:$PATH"

    cat > "$TEST_BIN/fakeharness" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "1.2.3-fake"
    exit 0
fi
EOF
    chmod +x "$TEST_BIN/fakeharness"

    ADAPTER="$SANDBOX/fakeharness-build-adapter.sh"
    cat > "$ADAPTER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
echo done > edited-by-fakeharness.txt
echo "adapter ran with flags: ${FAKEHARNESS_FLAGS:-}"
EOF
    chmod +x "$ADAPTER"
}

teardown() {
    [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

@test "missing --harness exits with a usage error" {
    run bash "$SCRIPT" --worktrees 1
    [ "$status" -eq 2 ]
    [[ "$output" == *"--harness"* ]]
}

@test "an unknown flag exits with a usage error" {
    run bash "$SCRIPT" --harness fakeharness --bogus-flag
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown argument"* ]]
}

@test "a synthetic harness completes an unattended edit via its own -build-adapter.sh convention" {
    run bash "$SCRIPT" --harness fakeharness --adapter "$ADAPTER" \
        --worktrees 1 --sandbox "$SANDBOX/run1" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run1/wt-1/edited-by-fakeharness.txt" ]
    grep -q '"harness": "fakeharness"' "$SANDBOX/evidence.json"
    grep -q '"fakeharness_version": "1.2.3-fake"' "$SANDBOX/evidence.json"
}

@test "--seed copies an extra tracked file into every worktree before the seed commit" {
    seed_src="$SANDBOX/extra-config.json"
    echo '{"seeded": true}' > "$seed_src"

    run bash "$SCRIPT" --harness fakeharness --adapter "$ADAPTER" \
        --seed "${seed_src}:extra-config.json" \
        --worktrees 2 --sandbox "$SANDBOX/run2" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    [ -f "$SANDBOX/run2/wt-1/extra-config.json" ]
    [ -f "$SANDBOX/run2/wt-2/extra-config.json" ]
    grep -q '"seeded": true' "$SANDBOX/run2/wt-1/extra-config.json"
    # The scratch repo root also carries the seeded file (it is a tracked
    # commit, not a per-worktree copy step) but stays otherwise untouched.
    [ -f "$SANDBOX/run2/scratch-repo/extra-config.json" ]
}

@test "--flags-env forwards the named environment variable to the adapter" {
    FAKEHARNESS_FLAGS="--custom-flag" run bash "$SCRIPT" --harness fakeharness --adapter "$ADAPTER" \
        --flags-env FAKEHARNESS_FLAGS \
        --worktrees 1 --sandbox "$SANDBOX/run3" --evidence-out "$SANDBOX/evidence.json"
    [ "$status" -eq 0 ]
    grep -q '"flags": "--custom-flag"' "$SANDBOX/evidence.json"
    grep -q 'adapter ran with flags: --custom-flag' "$SANDBOX/run3/wt-1.stdout"
}

@test "omitting --evidence-out defaults it to evidence.json under --sandbox" {
    # Every other test passes --evidence-out explicitly, leaving the script's
    # own default (`${sandbox}/evidence.json`) unexercised. --sandbox is still
    # pinned here (unlike --evidence-out) because BSD mktemp on macOS ignores
    # TMPDIR for a bare `mktemp -d`, unlike GNU mktemp in the Linux CI
    # container — asserting on that default's location would pass on one
    # platform and fail the other.
    run bash "$SCRIPT" --harness fakeharness --adapter "$ADAPTER" \
        --worktrees 1 --sandbox "$SANDBOX/run4"
    [ "$status" -eq 0 ]

    [ -f "$SANDBOX/run4/evidence.json" ]
    grep -q '"harness": "fakeharness"' "$SANDBOX/run4/evidence.json"
    [ -f "$SANDBOX/run4/wt-1/edited-by-fakeharness.txt" ]
}
