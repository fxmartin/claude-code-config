#!/usr/bin/env bash
# sweep-orphan-worktrees.sh
# Remove stale agent worktrees left behind by parallel /build-stories runs.
# Deletes any `.claude/worktrees/agent-*` directory older than 6 hours that
# is NOT currently registered with `git worktree list` (i.e. not in use by a
# live build). Safe to run mid-build — registered worktrees are never touched.
#
# Usage: sweep-orphan-worktrees.sh [repo-root]
#   repo-root defaults to the toplevel of the repo containing $PWD.
#
# Silent, non-blocking: a permission error on one orphan never aborts the
# sweep or fails the caller. Always exits 0.

set -u

MAX_AGE_MINUTES=360  # 6 hours

# Resolve the repo root: explicit arg, else the enclosing git toplevel.
REPO_ROOT="${1:-}"
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
[ -n "$REPO_ROOT" ] || exit 0

WORKTREE_DIR="$REPO_ROOT/.claude/worktrees"
[ -d "$WORKTREE_DIR" ] || exit 0

# Canonicalize a path (resolve symlinks like macOS /var -> /private/var) so
# git's worktree paths and find's results compare reliably.
_canon() {
    local p="$1"
    [ -e "$p" ] || { printf '%s\n' "$p"; return; }
    if [ -d "$p" ]; then
        ( cd "$p" 2>/dev/null && pwd -P ) || printf '%s\n' "$p"
    else
        printf '%s/%s\n' "$( cd "$(dirname "$p")" 2>/dev/null && pwd -P )" "$(basename "$p")"
    fi
}

# Collect the canonical paths git still considers live worktrees. A directory
# whose path appears here is in use by an active build and must be preserved.
IN_USE=""
if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    while IFS= read -r live; do
        [ -n "$live" ] || continue
        IN_USE+="$(_canon "$live")"$'\n'
    done < <(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null \
                | sed -n 's/^worktree //p')
fi

# Iterate stale `agent-*` directories older than 6 hours.
while IFS= read -r orphan; do
    [ -n "$orphan" ] || continue
    # Skip anything git still tracks as a live worktree (mid-build safety).
    if printf '%s' "$IN_USE" | grep -Fxq "$(_canon "$orphan")"; then
        continue
    fi
    # Best-effort removal. A locked/permission-denied directory is skipped,
    # never fatal — graceful degradation for unattended session-end runs.
    rm -rf "$orphan" 2>/dev/null || true
done < <(find "$WORKTREE_DIR" -mindepth 1 -maxdepth 1 -type d \
            -name 'agent-*' -mmin "+$MAX_AGE_MINUTES" 2>/dev/null)

exit 0
