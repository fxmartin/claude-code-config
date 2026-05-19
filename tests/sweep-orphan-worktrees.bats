#!/usr/bin/env bats
# Story 1.3-002 — worktree-leak bug.
# hooks/sweep-orphan-worktrees.sh removes .claude/worktrees/agent-* checkouts
# older than 6 hours, but never one whose branch is still registered with
# `git worktree list`. Tested in isolation against a throwaway repo so no
# real worktree on disk is ever touched.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    SWEEP="$REPO_ROOT/hooks/sweep-orphan-worktrees.sh"
    SANDBOX="$(mktemp -d)"
    REPO="$SANDBOX/repo"
    mkdir -p "$REPO"
    git -C "$REPO" init -q -b main
    git -C "$REPO" config user.email t@t.t
    git -C "$REPO" config user.name t
    echo seed > "$REPO/seed"
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m seed
    mkdir -p "$REPO/.claude/worktrees"
}

teardown() {
    [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

# Make a directory look older than 6h so the sweeper treats it as stale.
_age_dir() {
    touch -t "$(date -v-7H +%Y%m%d%H%M 2>/dev/null || date -d '7 hours ago' +%Y%m%d%H%M)" "$1"
}

@test "empty case: no worktrees, sweeper exits cleanly" {
    run bash "$SWEEP" "$REPO"
    [ "$status" -eq 0 ]
}

@test "stale case: an orphan agent dir older than 6h is removed" {
    orphan="$REPO/.claude/worktrees/agent-stale"
    mkdir -p "$orphan"
    echo data > "$orphan/file"
    _age_dir "$orphan"
    run bash "$SWEEP" "$REPO"
    [ "$status" -eq 0 ]
    [ ! -d "$orphan" ]
}

@test "fresh case: a recent orphan agent dir is kept" {
    fresh="$REPO/.claude/worktrees/agent-fresh"
    mkdir -p "$fresh"
    run bash "$SWEEP" "$REPO"
    [ "$status" -eq 0 ]
    [ -d "$fresh" ]
}

@test "mid-build case: an in-use worktree is never removed even when old" {
    wt="$REPO/.claude/worktrees/agent-inuse"
    git -C "$REPO" worktree add -q -b feat/inuse "$wt" >/dev/null
    _age_dir "$wt"
    run bash "$SWEEP" "$REPO"
    [ "$status" -eq 0 ]
    [ -d "$wt" ]
    git -C "$REPO" worktree list --porcelain | grep -q "agent-inuse"
}

@test "permission-error case: undeletable orphan is skipped gracefully" {
    orphan="$REPO/.claude/worktrees/agent-locked"
    mkdir -p "$orphan/sub"
    _age_dir "$orphan"
    chmod 000 "$orphan"
    run bash "$SWEEP" "$REPO"
    chmod 755 "$orphan" 2>/dev/null || true
    # Sweeper must not abort the run on a single failed removal.
    [ "$status" -eq 0 ]
}

@test "no-git-repo: sweeper exits 0 when passed a plain directory" {
    # REPO_ROOT is not a git repo — sweeper must degrade silently.
    plain_dir="$(mktemp -d)"
    mkdir -p "$plain_dir/.claude/worktrees"
    orphan="$plain_dir/.claude/worktrees/agent-norepo"
    mkdir -p "$orphan"
    # Age it so it would be swept if the git guard were absent.
    touch -t "$(date -v-7H +%Y%m%d%H%M 2>/dev/null || date -d '7 hours ago' +%Y%m%d%H%M)" "$orphan"
    run bash "$SWEEP" "$plain_dir"
    rm -rf "$plain_dir"
    [ "$status" -eq 0 ]
}
