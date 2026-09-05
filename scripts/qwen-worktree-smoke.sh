#!/usr/bin/env bash
#
# ABOUTME: Story 29.1-001 evidence entry point — a thin wrapper around the
# ABOUTME: shared scripts/harness-worktree-smoke.sh, pinned to the qwen adapter.
#
# Story 29.2-002 generalized this script's original qwen-only implementation
# into scripts/harness-worktree-smoke.sh (parameterized by --harness), so
# opencode's equivalent evidence script (scripts/opencode-worktree-smoke.sh)
# shares the same scratch-repo / concurrent-worktree / isolation-check logic
# instead of duplicating it. This wrapper keeps the qwen-specific CLI surface
# unchanged for existing callers and tests/qwen-worktree-smoke.bats.
#
# Usage:
#   qwen-worktree-smoke.sh [--worktrees N] [--sandbox DIR] [--evidence-out FILE]
#
# Environment:
#   QWEN_FLAGS  Forwarded to scripts/qwen-build-adapter.sh (e.g. '--yolo' for
#               unattended approval — see that script's own docs).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "${SCRIPT_DIR}/harness-worktree-smoke.sh" --harness qwen "$@"
