#!/usr/bin/env bash
# ABOUTME: Structural verification of the fx-claude-config plugin marketplace
# ABOUTME: (Story 6.4-001 — path B: `/plugin marketplace add` install path).
#
# Companion to scripts/smoke-test.sh:
#
#   - scripts/smoke-test.sh           → path A (install.sh --core symlinks).
#   - scripts/verify-plugin-install.sh → path B (GitHub-direct marketplace).
#
# What this script does
# ─────────────────────
# It does NOT actually invoke Claude Code or call `/plugin marketplace add`
# (impossible in CI). Instead it validates the on-disk structure that a
# real Claude Code instance would consume when a user runs
#     /plugin marketplace add fxmartin/claude-code-config
#     /plugin install autonomous-sdlc@fx-claude-config
#
# Validations performed:
#   1. `.claude-plugin/marketplace.json` exists and is valid JSON.
#   2. Required top-level keys are present (name, plugins).
#   3. Every plugin declared in `plugins[]`:
#      a. Has a `source` path that resolves to a real directory in the repo.
#      b. Contains `.claude-plugin/plugin.json` (valid JSON, name+version+description).
#      c. Contains a non-empty `skills/` directory.
#      d. Every `skills/<name>/SKILL.md` declares `name: <dir>` in frontmatter.
#
# Output contract: prints per-check lines and the summary
#     VERIFY_PLUGIN: <pass>/<total> passed
# which CI greps for and tests/plugin-install-paths.bats asserts on.
#
# Env overrides (testing hooks):
#   VERIFY_PLUGIN_ROOT_OVERRIDE — repo root to validate. Defaults to the
#     parent of this script's directory. Used by bats to feed synthetic
#     fixtures without staging real files.
#
# Exit codes:
#   0 — every check passed
#   1 — one or more checks failed
#   2 — preflight failure (missing jq, etc.)

set -euo pipefail

# ─── Usage ───────────────────────────────────────────────────────────────────
usage() {
  cat <<'USAGE'
Usage: verify-plugin-install.sh [--help]

Validates the fx-claude-config marketplace structure that Claude Code
consumes via `/plugin marketplace add fxmartin/claude-code-config`.

This is structural verification only — it does NOT invoke Claude Code.
Use scripts/smoke-test.sh for the local install.sh path; both scripts
together cover Story 6.4-001's two install paths.

Options:
  --help, -h    Show this help and exit 0.

Env (testing):
  VERIFY_PLUGIN_ROOT_OVERRIDE  Repo root to validate (defaults to repo).

Exit codes:
  0  every check passed
  1  one or more checks failed
  2  preflight failure (missing jq, etc.)
USAGE
}

case "${1:-}" in
  --help|-h) usage; exit 0 ;;
esac

# ─── Preflight ───────────────────────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
  echo "FAIL preflight: jq is required but not installed" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${VERIFY_PLUGIN_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MARKETPLACE_JSON="$REPO_ROOT/.claude-plugin/marketplace.json"

echo "verify-plugin-install: REPO_ROOT=$REPO_ROOT"

# ─── Test bookkeeping ────────────────────────────────────────────────────────
TOTAL=0
PASSED=0
FAILED_CHECKS=()

record() {
  local name="$1"
  local result="$2"
  local detail="${3:-}"
  TOTAL=$((TOTAL + 1))
  if [ "$result" = "pass" ]; then
    PASSED=$((PASSED + 1))
    echo "  ok   $name"
  else
    FAILED_CHECKS+=("$name")
    echo "  FAIL $name${detail:+ — $detail}" >&2
  fi
}

# ─── Phase 1: marketplace manifest ───────────────────────────────────────────
echo ""
echo "[phase 1] marketplace manifest"

if [ -f "$MARKETPLACE_JSON" ]; then
  record "marketplace manifest exists" pass
else
  record "marketplace manifest exists" fail "$MARKETPLACE_JSON"
  echo ""
  echo "VERIFY_PLUGIN: ${PASSED}/${TOTAL} passed"
  exit 1
fi

if jq -e . "$MARKETPLACE_JSON" >/dev/null 2>&1; then
  record "marketplace manifest is valid JSON" pass
else
  record "marketplace manifest is valid JSON" fail "jq parse failed"
  echo ""
  echo "VERIFY_PLUGIN: ${PASSED}/${TOTAL} passed"
  exit 1
fi

# Required top-level keys.
mp_name="$(jq -r '.name // empty' "$MARKETPLACE_JSON")"
if [ -n "$mp_name" ]; then
  record "marketplace manifest declares .name=$mp_name" pass
else
  record "marketplace manifest declares .name" fail "missing or empty"
fi

plugin_count="$(jq -r '.plugins | length' "$MARKETPLACE_JSON" 2>/dev/null || echo 0)"
if [ "$plugin_count" -gt 0 ] 2>/dev/null; then
  record "marketplace declares at least one plugin (count=$plugin_count)" pass
else
  record "marketplace declares at least one plugin" fail "plugins[] empty or missing"
  echo ""
  echo "VERIFY_PLUGIN: ${PASSED}/${TOTAL} passed"
  exit 1
fi

# ─── Phase 2: per-plugin validation ──────────────────────────────────────────
echo ""
echo "[phase 2] per-plugin validation"

# Pull the plugin list as a TSV (name<TAB>source) to avoid jq subshell churn.
plugins_tsv="$(jq -r '.plugins[] | [.name, .source] | @tsv' "$MARKETPLACE_JSON")"

while IFS=$'\t' read -r plugin_name plugin_source; do
  [ -z "$plugin_name" ] && continue

  # source is repo-relative ("./plugins/autonomous-sdlc"); strip the leading "./"
  plugin_dir="$REPO_ROOT/${plugin_source#./}"

  # 2a. Plugin source directory must exist.
  if [ -d "$plugin_dir" ]; then
    record "[$plugin_name] source path resolves ($plugin_source)" pass
  else
    record "[$plugin_name] source path resolves ($plugin_source)" fail "directory not found: $plugin_dir"
    continue
  fi

  # 2b. Plugin manifest must exist and be valid JSON with required keys.
  plugin_json="$plugin_dir/.claude-plugin/plugin.json"
  if [ -f "$plugin_json" ]; then
    record "[$plugin_name] plugin.json exists" pass
  else
    record "[$plugin_name] plugin.json exists" fail "$plugin_json"
    continue
  fi

  if jq -e . "$plugin_json" >/dev/null 2>&1; then
    record "[$plugin_name] plugin.json is valid JSON" pass
  else
    record "[$plugin_name] plugin.json is valid JSON" fail "jq parse failed"
    continue
  fi

  missing_keys=""
  for key in name version description; do
    val="$(jq -r ".${key} // empty" "$plugin_json")"
    if [ -z "$val" ]; then
      missing_keys="${missing_keys} ${key}"
    fi
  done
  if [ -z "$missing_keys" ]; then
    record "[$plugin_name] plugin.json declares name+version+description" pass
  else
    record "[$plugin_name] plugin.json declares name+version+description" fail "missing:${missing_keys}"
  fi

  # 2c. Skills directory must exist and be non-empty.
  skills_dir="$plugin_dir/skills"
  if [ ! -d "$skills_dir" ]; then
    record "[$plugin_name] skills/ directory exists" fail "$skills_dir"
    continue
  fi
  record "[$plugin_name] skills/ directory exists" pass

  # Count skill subdirectories that contain a SKILL.md.
  skill_count=0
  for skill_path in "$skills_dir"/*/; do
    [ -d "$skill_path" ] || continue
    [ -f "$skill_path/SKILL.md" ] || continue
    skill_count=$((skill_count + 1))
  done
  if [ "$skill_count" -gt 0 ]; then
    record "[$plugin_name] has $skill_count skill(s) with SKILL.md" pass
  else
    record "[$plugin_name] has at least one skill" fail "no skills/<name>/SKILL.md found"
    continue
  fi

  # 2d. Each SKILL.md must declare `name: <dir>` in frontmatter.
  bad_frontmatter=""
  for skill_path in "$skills_dir"/*/; do
    [ -d "$skill_path" ] || continue
    skill_md="$skill_path/SKILL.md"
    [ -f "$skill_md" ] || continue
    skill_name="$(basename "$skill_path")"

    # The frontmatter `name:` value must match the directory name; tools
    # parse it to wire up the slash-command.
    if ! grep -qE "^name:[[:space:]]*${skill_name}[[:space:]]*$" "$skill_md"; then
      bad_frontmatter="${bad_frontmatter} ${skill_name}"
    fi
  done

  if [ -z "$bad_frontmatter" ]; then
    record "[$plugin_name] every SKILL.md frontmatter name matches its dir" pass
  else
    record "[$plugin_name] every SKILL.md frontmatter name matches its dir" \
           fail "mismatched:${bad_frontmatter}"
  fi

  # Report the skill names for human-readable + grep-friendly output.
  for skill_path in "$skills_dir"/*/; do
    [ -d "$skill_path" ] || continue
    [ -f "$skill_path/SKILL.md" ] || continue
    echo "      skill: $(basename "$skill_path")"
  done

done <<EOF
$plugins_tsv
EOF

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "VERIFY_PLUGIN: ${PASSED}/${TOTAL} passed"

if [ "$PASSED" -eq "$TOTAL" ]; then
  exit 0
else
  echo "failed checks: ${FAILED_CHECKS[*]}" >&2
  exit 1
fi
