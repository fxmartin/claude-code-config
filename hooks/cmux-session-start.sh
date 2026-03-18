#!/bin/bash
# Hook: SessionStart — Rename workspace to repo/folder name, log session start.
cat > /dev/null

BRIDGE="$HOME/.claude/hooks/cmux-bridge.sh"

# Rename workspace: repo name if git repo, otherwise folder name
REPO_NAME=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null)
WORKSPACE_NAME="${REPO_NAME:-$(basename "$PWD")}"

if command -v cmux &>/dev/null && [ -n "${CMUX_SOCKET_PATH:-}" ]; then
    cmux workspace-action --action rename --title "$WORKSPACE_NAME" 2>/dev/null || true
fi

"$BRIDGE" log info "Claude session started" --source claude
