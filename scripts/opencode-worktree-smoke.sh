#!/usr/bin/env bash
#
# ABOUTME: Story 29.2-002 evidence entry point — a thin wrapper around the
# ABOUTME: shared scripts/harness-worktree-smoke.sh, pinned to the opencode adapter.
#
# Seeds the scratch repo with an opencode.json granting
# {"permission": {"edit": "allow", "bash": "allow"}} — REQUIRED for
# unattended dispatch (see scripts/opencode-build-adapter.sh and
# harnesses.yaml's opencode entry): without it OpenCode blocks a headless run
# on an interactive approval prompt it has no TTY to answer, and the run
# hangs indefinitely rather than failing fast (verified against real OpenCode
# 1.18.15). Because opencode.json is committed as a tracked file in the
# scratch repo's seed commit, every `git worktree add` inherits it
# automatically — no per-worktree setup step is needed.
#
# Usage:
#   opencode-worktree-smoke.sh [--worktrees N] [--sandbox DIR] [--evidence-out FILE]
#
# Environment:
#   OPENCODE_FLAGS  Forwarded to scripts/opencode-build-adapter.sh, e.g.
#                   '--model anthropic/claude-haiku-4-5' — REQUIRED whenever
#                   this host's OpenCode default model is unreachable (see
#                   harnesses.yaml's opencode entry); otherwise the adapter
#                   blocks on the captured dispatch path's 3600s backstop
#                   instead of completing the task.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

permission_config_dir="$(mktemp -d)"
permission_config="${permission_config_dir}/opencode.json"
printf '{\n  "permission": { "edit": "allow", "bash": "allow" }\n}\n' > "$permission_config"

exec "${SCRIPT_DIR}/harness-worktree-smoke.sh" \
  --harness opencode \
  --seed "${permission_config}:opencode.json" \
  "$@"
