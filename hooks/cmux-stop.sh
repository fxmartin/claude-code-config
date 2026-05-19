#!/bin/bash
# Hook: Stop — Fires after each Claude response completes.
# Clear progress bar and any stale permission pill, then tear down completed
# parallel-build worktrees so long runs do not leak orphan checkouts on disk.
# When a skill is active (sentinel file exists), skip progress clear — skill manages it.

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

    local wt branch
    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        case "$wt" in
            */.claude/worktrees/agent-*) ;;
            *) continue ;;
        esac
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

cat > /dev/null
SKILL_ACTIVE=$(cat /tmp/.claude-skill-active 2>/dev/null)

if [ -z "$SKILL_ACTIVE" ]; then
    ~/.claude/hooks/cmux-bridge.sh clear
fi
~/.claude/hooks/cmux-bridge.sh clear claude

# Session-end worktree cleanup — never blocks the Stop hook.
_prune_worktrees "" 2>/dev/null || true
