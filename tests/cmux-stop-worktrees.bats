#!/usr/bin/env bats
# Story 1.3-002 — cmux-stop.sh removes merged worktrees.
# At session end cmux-stop.sh must tear down any .claude/worktrees/agent-*
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
    run bash "$REPO_ROOT/hooks/cmux-stop.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    [ ! -d "$wt" ]
    ! git -C "$REPO" worktree list --porcelain | grep -q "agent-merged"
}

@test "unmerged worktree branch is preserved" {
    wt="$REPO/.claude/worktrees/agent-unmerged"
    git -C "$REPO" worktree add -q -b feat/unmerged "$wt" >/dev/null
    echo wip > "$wt/seed"
    git -C "$wt" commit -q -am wip
    run bash "$REPO_ROOT/hooks/cmux-stop.sh" prune-worktrees "$REPO"
    [ "$status" -eq 0 ]
    [ -d "$wt" ]
}
