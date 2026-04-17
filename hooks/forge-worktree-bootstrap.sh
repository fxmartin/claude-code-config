#!/usr/bin/env bash
# forge-worktree-bootstrap.sh
# When Claude starts a session inside a git worktree under
# <repo>/.claude/worktrees/agent-*, propagate .claude/settings.local.json
# from the parent checkout so Bash permissions are inherited.
# tracked .claude/settings.json is carried by git itself.
# Silent no-op on error — must never block session start.

set -u
cwd="$(pwd 2>/dev/null || true)"
case "$cwd" in
  */.claude/worktrees/agent-*)
    parent="${cwd%/.claude/worktrees/*}"
    src="$parent/.claude/settings.local.json"
    dst_dir="$cwd/.claude"
    dst="$dst_dir/settings.local.json"
    if [ -f "$src" ] && [ ! -f "$dst" ]; then
      mkdir -p "$dst_dir" 2>/dev/null || exit 0
      cp "$src" "$dst" 2>/dev/null || exit 0
    fi
    ;;
esac
exit 0
