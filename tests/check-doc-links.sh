#!/usr/bin/env bash
# check-doc-links.sh — verify every local file reference in the core docs resolves.
#
# Story 1.2-001: Resolve WORKFLOW.md and workflow-diagram.png dangling references.
# Scoped to the documentation touched by that story; not a project-wide test harness.
#
# Checks two kinds of references in each target file:
#   1. Markdown links/images:  [text](path) and ![alt](path)
#   2. Inline-code path mentions of WORKFLOW*.md inside backticks
# External (http/https) links and pure-anchor links (#section) are skipped.
#
# Exit 0 if all references resolve, 1 otherwise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Files this story is responsible for keeping link-clean.
TARGETS=(
  "CLAUDE.md"
  "README.md"
  "WORKFLOW-v2.md"
  "docs/claude-md-guide.md"
)

failures=0

# Resolve a candidate path that appears inside a given source file.
# Relative links resolve against the source file's directory.
resolve() {
  local src_file="$1" ref="$2"
  # Strip any trailing #anchor and surrounding whitespace.
  ref="${ref%%#*}"
  [ -z "$ref" ] && return 0
  case "$ref" in
    http://*|https://*|mailto:*) return 0 ;;
  esac
  local base_dir
  base_dir="$(dirname "$src_file")"
  if [ -e "$base_dir/$ref" ]; then
    return 0
  fi
  echo "  BROKEN: $src_file -> $ref"
  return 1
}

for f in "${TARGETS[@]}"; do
  if [ ! -f "$f" ]; then
    echo "  BROKEN: target file missing -> $f"
    failures=$((failures + 1))
    continue
  fi

  # 1. Markdown link / image targets: ](...)
  while IFS= read -r ref; do
    [ -z "$ref" ] && continue
    resolve "$f" "$ref" || failures=$((failures + 1))
  done < <(grep -oE '\]\([^)]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//')

  # 2. Backticked WORKFLOW*.md path mentions in prose.
  # Only checked in CLAUDE.md, where such mentions are authoritative
  # root-relative references. Other docs may quote those paths verbatim.
  if [ "$f" = "CLAUDE.md" ]; then
    # shellcheck disable=SC2016  # backticks below are literal regex chars, not command substitution
    while IFS= read -r ref; do
      [ -z "$ref" ] && continue
      resolve "$f" "$ref" || failures=$((failures + 1))
    done < <(grep -oE '`WORKFLOW[A-Za-z0-9.-]*\.md`' "$f" | tr -d '`' | sort -u)
  fi
done

if [ "$failures" -ne 0 ]; then
  echo "FAIL: $failures broken reference(s)."
  exit 1
fi

echo "OK: all documentation references resolve."
