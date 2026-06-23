#!/usr/bin/env bats
# Story 1.3-002 / #142 — worktree-gc.sh removes merged worktrees.
# At session end worktree-gc.sh must tear down any .claude/worktrees/agent-*
# whose branch is fully merged into main, then delegate stale-orphan
# cleanup to sweep-orphan-worktrees.sh. Exercised against a throwaway repo.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
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

@test "merged worktree branch is torn down" {
    wt="$REPO/.claude/worktrees/agent-merged"
    git -C "$REPO" worktree add -q -b feat/merged "$wt" >/dev/null
    echo change > "$wt/seed"
    git -C "$wt" commit -q -am change
    git -C "$REPO" merge -q feat/merged
    run bash "$REPO_ROOT/hooks/worktree-gc.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    [ ! -d "$wt" ]
    ! git -C "$REPO" worktree list --porcelain | grep -q "agent-merged"
}

@test "unmerged worktree branch is preserved" {
    wt="$REPO/.claude/worktrees/agent-unmerged"
    git -C "$REPO" worktree add -q -b feat/unmerged "$wt" >/dev/null
    echo wip > "$wt/seed"
    git -C "$wt" commit -q -am wip
    run bash "$REPO_ROOT/hooks/worktree-gc.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    [ -d "$wt" ]
}

@test "detached-HEAD worktree is skipped, not removed" {
    wt="$REPO/.claude/worktrees/agent-detached"
    git -C "$REPO" worktree add -q -b feat/detach "$wt" >/dev/null
    echo change > "$wt/seed"
    git -C "$wt" commit -q -am change
    # Detach HEAD so symbolic-ref returns nothing
    git -C "$wt" checkout -q --detach >/dev/null
    git -C "$REPO" merge -q feat/detach
    run bash "$REPO_ROOT/hooks/worktree-gc.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    # Detached HEAD: _prune_worktrees skips the branch-check, directory survives.
    [ -d "$wt" ]
}

@test "non-agent worktree pattern is never removed" {
    # A worktree outside the agent-* naming convention must never be swept.
    wt="$REPO/.claude/worktrees/shared-tools"
    git -C "$REPO" worktree add -q -b feat/shared "$wt" >/dev/null
    echo change > "$wt/seed"
    git -C "$wt" commit -q -am change
    git -C "$REPO" merge -q feat/shared
    run bash "$REPO_ROOT/hooks/worktree-gc.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    # Pattern guard `*/.claude/worktrees/agent-*` excludes this directory.
    [ -d "$wt" ]
}

@test "direct invocation with explicit repo-root removes merged worktree" {
    # The Stop-hook path resolves the repo via git; the direct form takes an
    # explicit repo-root argument (no `prune-worktrees` subcommand).
    wt="$REPO/.claude/worktrees/agent-direct"
    git -C "$REPO" worktree add -q -b feat/direct "$wt" >/dev/null
    echo change > "$wt/seed"
    git -C "$wt" commit -q -am change
    git -C "$REPO" merge -q feat/direct
    run bash "$REPO_ROOT/hooks/worktree-gc.sh" "$REPO"
    [ "$status" -eq 0 ]
    [ ! -d "$wt" ]
}
