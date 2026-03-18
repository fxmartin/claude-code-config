#!/bin/bash
# Hook: Stop — Fires after each Claude response completes.
# Clear progress bar and any stale permission pill.
cat > /dev/null
~/.claude/hooks/cmux-bridge.sh clear
~/.claude/hooks/cmux-bridge.sh clear claude
