#!/bin/bash
# Hook: Stop — Fires after each Claude response completes.
# Only clear progress bar. No logs, no status, no notifications.
# Meaningful events are logged by agent hooks and skill phase transitions.
cat > /dev/null
~/.claude/hooks/cmux-bridge.sh clear
