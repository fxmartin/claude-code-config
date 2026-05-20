#!/usr/bin/env bash
# ABOUTME: Clean-machine install smoke test (Story 3.2-002).
# ABOUTME: Exercises install.sh --core through dry-run, install, idempotent, uninstall.
#
# The smoke test runs in CI on macOS-latest and ubuntu-latest GitHub runners
# and verifies that the modal installer behaves correctly on a pristine box:
#
#   1. `./install.sh --core --dry-run` produces preview output and exit 0.
#   2. `./install.sh --core` creates the expected symlinks and exits 0.
#   3. A second `./install.sh --core` is idempotent (no filesystem mutations).
#   4. `./install.sh --uninstall` removes the symlinks and exits 0.
#
# All four phases run inside an isolated `$HOME` (mktemp) so the runner's real
# home is never touched. The summary line `SMOKE_TEST: <pass>/<total> passed`
# is the contract CI greps for and the bats suite asserts on.
#
# Env overrides (testing hooks — not for users):
#   SMOKE_HOME_OVERRIDE        — caller-provided temp HOME; smoke-test will
#                                NOT delete it on exit so the test can inspect
#                                the post-uninstall filesystem state.
#   SMOKE_SCRIPT_ROOT_OVERRIDE — repo root to source install.sh from. Defaults
#                                to the parent of this script's directory.
#
# Exit codes:
#   0  — every phase passed
#   1  — one or more phases failed (see summary line)
#   2  — preflight failure (missing install.sh, missing tools)

set -euo pipefail

# ─── Locate repo root ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_ROOT="${SMOKE_SCRIPT_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTALL_SH="$SCRIPT_ROOT/install.sh"

if [ ! -x "$INSTALL_SH" ]; then
  echo "✗ smoke-test: install.sh not found or not executable at $INSTALL_SH" >&2
  exit 2
fi

# ─── Set up isolated HOME ────────────────────────────────────────────────
# When SMOKE_HOME_OVERRIDE is set, the caller (bats) owns cleanup so it can
# inspect leftover state. Otherwise we mktemp and trap-clean.
if [ -n "${SMOKE_HOME_OVERRIDE:-}" ]; then
  SMOKE_HOME="$SMOKE_HOME_OVERRIDE"
  mkdir -p "$SMOKE_HOME"
  OWNS_TEMP_HOME=false
else
  SMOKE_HOME="$(mktemp -d -t smoke-claude-XXXXXX)"
  OWNS_TEMP_HOME=true
fi

cleanup() {
  if [ "$OWNS_TEMP_HOME" = "true" ] && [ -n "${SMOKE_HOME:-}" ] && [ -d "$SMOKE_HOME" ]; then
    rm -rf "$SMOKE_HOME"
  fi
}
trap cleanup EXIT

export SMOKE_HOME

echo "smoke-test: SCRIPT_ROOT=$SCRIPT_ROOT"
echo "smoke-test: SMOKE_HOME=$SMOKE_HOME"

# ─── Test bookkeeping ────────────────────────────────────────────────────
TOTAL=0
PASSED=0
FAILED_PHASES=()

# Pretty-print one phase result. `record <name> <pass|fail> [detail]`
record() {
  local name="$1"
  local result="$2"
  local detail="${3:-}"
  TOTAL=$((TOTAL + 1))
  if [ "$result" = "pass" ]; then
    PASSED=$((PASSED + 1))
    echo "  ✓ $name"
  else
    FAILED_PHASES+=("$name")
    echo "  ✗ $name${detail:+ — $detail}" >&2
  fi
}

# Run install.sh with the isolated HOME and return its exit status + output
# via the bats `run`-style globals SMOKE_STATUS / SMOKE_OUTPUT.
SMOKE_STATUS=0
SMOKE_OUTPUT=""
run_install() {
  set +e
  SMOKE_OUTPUT="$(env HOME="$SMOKE_HOME" CLAUDE_CONFIG_NO_ENV=1 \
                      bash "$INSTALL_SH" "$@" 2>&1)"
  SMOKE_STATUS=$?
  set -e
}

# Snapshot every entry under the isolated HOME so we can compare before/after
# an idempotent run. Sort for byte-stable output.
snapshot_home() {
  if [ -d "$SMOKE_HOME" ]; then
    find "$SMOKE_HOME" 2>/dev/null | LC_ALL=C sort
  fi
}

# ─── Phase 1: dry-run ────────────────────────────────────────────────────
echo ""
echo "[phase 1] --core --dry-run"
run_install --core --dry-run
if [ "$SMOKE_STATUS" -eq 0 ] && [ -n "$SMOKE_OUTPUT" ]; then
  record "dry-run exits 0 with non-empty output" pass
else
  record "dry-run exits 0 with non-empty output" fail "exit=$SMOKE_STATUS, output_bytes=${#SMOKE_OUTPUT}"
fi

# Dry-run must not have created the target directory.
if [ ! -e "$SMOKE_HOME/.claude" ]; then
  record "dry-run does not touch the filesystem" pass
else
  record "dry-run does not touch the filesystem" fail "$SMOKE_HOME/.claude exists"
fi

# ─── Phase 2: actual install ─────────────────────────────────────────────
echo ""
echo "[phase 2] --core install"
run_install --core
if [ "$SMOKE_STATUS" -eq 0 ]; then
  record "install exits 0" pass
else
  record "install exits 0" fail "exit=$SMOKE_STATUS"
fi

# Spot-check three representative symlinks: top-level file, top-level dir,
# nested marketplace symlink. If these are right, the rest are right by
# construction (core.sh links them all in a single function call).
expected_links=(
  "$SMOKE_HOME/.claude/CLAUDE.md"
  "$SMOKE_HOME/.claude/agents"
  "$SMOKE_HOME/.claude/plugins/marketplaces/fx-claude-config"
)
links_ok=true
for link in "${expected_links[@]}"; do
  if [ ! -L "$link" ]; then
    links_ok=false
    echo "    expected symlink missing: $link" >&2
  fi
done
if $links_ok; then
  record "install creates expected symlinks" pass
else
  record "install creates expected symlinks" fail
fi

# ─── Phase 3: idempotent re-run ──────────────────────────────────────────
echo ""
echo "[phase 3] --core idempotent re-run"
before="$(snapshot_home)"
run_install --core
after="$(snapshot_home)"
if [ "$SMOKE_STATUS" -eq 0 ]; then
  record "idempotent re-run exits 0" pass
else
  record "idempotent re-run exits 0" fail "exit=$SMOKE_STATUS"
fi
if [ "$before" = "$after" ]; then
  record "idempotent re-run produces no new changes" pass
else
  record "idempotent re-run produces no new changes" fail "filesystem snapshot differs"
fi

# ─── Phase 4: uninstall ──────────────────────────────────────────────────
echo ""
echo "[phase 4] --uninstall"
run_install --uninstall
if [ "$SMOKE_STATUS" -eq 0 ]; then
  record "uninstall exits 0" pass
else
  record "uninstall exits 0" fail "exit=$SMOKE_STATUS"
fi

uninstall_clean=true
for link in "${expected_links[@]}"; do
  if [ -L "$link" ]; then
    uninstall_clean=false
    echo "    symlink not removed: $link" >&2
  fi
done
if $uninstall_clean; then
  record "uninstall removes symlinks" pass
else
  record "uninstall removes symlinks" fail
fi

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "SMOKE_TEST: ${PASSED}/${TOTAL} passed"

if [ "$PASSED" -eq "$TOTAL" ]; then
  exit 0
else
  echo "failed phases: ${FAILED_PHASES[*]}" >&2
  exit 1
fi
