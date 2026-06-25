#!/bin/bash
# ABOUTME: cmux-independent worktree GC — relocated out of cmux-stop.sh (#142).
# Hook: Stop — Tears down completed parallel-build worktrees so long runs do not
# leak orphan checkouts on disk. Removes any `.claude/worktrees/agent-*` whose
# branch is fully merged into main, prunes, then sweeps stale orphans (>6h).
# Silent, non-blocking — a failure here must never break session end.
#
# Invocation:
#   worktree-gc.sh                     — Stop hook: read+discard stdin, resolve
#                                        repo via `git rev-parse`, run GC.
#   worktree-gc.sh [repo-root]         — direct: GC against an explicit repo.
#   worktree-gc.sh prune-worktrees [repo-root]
#                                      — subcommand form, for tests / manual runs.
# Always exits 0.

set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Remove any `.claude/worktrees/agent-*` whose branch is fully merged into
# main, then sweep stale orphans (>6h) regardless of merge state.
# Silent, non-blocking — a failure here must never break session end.
_prune_worktrees() {
    local repo_root="${1:-}"
    if [ -z "$repo_root" ]; then
        repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    fi
    [ -n "$repo_root" ] || return 0
    git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1 || return 0

    # Locked worktrees are in use by a live build (#180): the controller locks
    # each story worktree it owns. A story's feature branch is 0 commits ahead of
    # main until the build agent commits late, so the merge check below would
    # otherwise reap a still-in-use checkout. `git worktree list --porcelain`
    # emits a `locked` line in a locked worktree's record — collect those paths.
    local locked_paths
    locked_paths="$(git -C "$repo_root" worktree list --porcelain 2>/dev/null \
        | awk '/^worktree / { wt = substr($0, 10) } /^locked/ { print wt }')"

    local wt branch
    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        case "$wt" in
            */.claude/worktrees/agent-*) ;;
            *) continue ;;
        esac
        # Skip worktrees git reports as locked — a live build owns them (#180).
        if printf '%s\n' "$locked_paths" | grep -Fxq "$wt"; then
            continue
        fi
        # Branch backing this worktree, e.g. "refs/heads/feature/x".
        branch="$(git -C "$wt" symbolic-ref --quiet HEAD 2>/dev/null || true)"
        [ -n "$branch" ] || continue
        # Keep it unless the branch is fully merged into main. `git branch`
        # prefixes the line with `* ` (current), `+ ` (checked out in another
        # worktree), or two spaces — strip all three markers before matching.
        if git -C "$repo_root" branch --merged main 2>/dev/null \
            | sed 's/^[*+ ]*//' | grep -Fxq "${branch#refs/heads/}"; then
            git -C "$repo_root" worktree remove --force "$wt" 2>/dev/null || true
        fi
    done < <(git -C "$repo_root" worktree list --porcelain 2>/dev/null \
                | sed -n 's/^worktree //p')

    git -C "$repo_root" worktree prune 2>/dev/null || true
    bash "$HOOK_DIR/sweep-orphan-worktrees.sh" "$repo_root" 2>/dev/null || true
}

# `prune-worktrees [repo-root]` — directly invokable for tests / manual runs.
if [ "${1:-}" = "prune-worktrees" ]; then
    _prune_worktrees "${2:-}"
    exit 0
fi

# Direct invocation with an explicit repo root: `worktree-gc.sh [repo-root]`.
if [ -n "${1:-}" ]; then
    _prune_worktrees "$1"
    exit 0
fi

# Stop-hook path — discard stdin, resolve repo via git, never block.
cat > /dev/null
_prune_worktrees "" 2>/dev/null || true
exit 0
