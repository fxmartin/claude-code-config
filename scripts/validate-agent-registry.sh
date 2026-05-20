#!/usr/bin/env bash
#
# validate-agent-registry.sh — Agent-registry validator (Story 2.1-003).
#
# Greps every `*.md` under plugins/, skills/, commands/ for `subagent_type=`
# references and resolves each referenced agent name against the basenames of
# files in agents/ (including subdirectories such as agents/personal/, added
# by Story 6.2-001 to separate personal helpers from the plugin scope).
# Built-in Claude Code subagent types are allowlisted.
#
# A reference whose name is a bracketed placeholder (e.g. [story.agent_type],
# [AGENT_TYPE]) is a variable, not a literal agent name — it is skipped.
#
# Exit status:
#   0  all literal references resolve (or there are none)
#   1  one or more references are unresolved (details printed to stderr)
#   2  usage / environment error
#
# Usage:
#   scripts/validate-agent-registry.sh [REPO_ROOT]
#
# REPO_ROOT defaults to the git toplevel containing this script.

set -euo pipefail

# --- Locate the repository root -------------------------------------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="${1:-}"
if [ -z "${repo_root}" ]; then
  repo_root="$(cd "${script_dir}/.." && pwd)"
fi

if [ ! -d "${repo_root}/agents" ]; then
  echo "error: agents/ directory not found under ${repo_root}" >&2
  exit 2
fi

# --- Built-in subagent types (always valid, no agents/ file expected) -----
# Claude Code ships these subagent types; they are not project agent files.
builtin_types=(
  "general-purpose"
  "Plan"
  "Explore"
)

is_builtin() {
  local name="$1"
  local builtin
  for builtin in "${builtin_types[@]}"; do
    if [ "${name}" = "${builtin}" ]; then
      return 0
    fi
  done
  return 1
}

# --- Collect known agent basenames (without .md suffix) -------------------
# Walks agents/ recursively so personal helpers under agents/personal/ are
# still resolvable targets for subagent_type= references.
known_agents=()
while IFS= read -r agent_file; do
  known_agents+=("$(basename "${agent_file}" .md)")
done < <(find "${repo_root}/agents" -type f -name '*.md' | sort)

is_known_agent() {
  local name="$1"
  local agent
  for agent in "${known_agents[@]}"; do
    if [ "${name}" = "${agent}" ]; then
      return 0
    fi
  done
  return 1
}

# --- Scan for subagent_type= references -----------------------------------
unresolved=0

for dir in plugins skills commands; do
  search_dir="${repo_root}/${dir}"
  [ -d "${search_dir}" ] || continue

  while IFS= read -r md_file; do
    line_no=0
    while IFS= read -r line; do
      line_no=$((line_no + 1))
      case "${line}" in
        *subagent_type=*) ;;
        *) continue ;;
      esac

      # Extract every subagent_type= reference on the line.
      # A reference value may be: "name" | 'name' | bare-name | [placeholder]
      while IFS= read -r ref; do
        [ -n "${ref}" ] || continue

        # Strip the `subagent_type=` prefix and any surrounding quotes.
        value="${ref#*subagent_type=}"
        value="${value#\"}"
        value="${value#\'}"
        value="${value%\"}"
        value="${value%\'}"

        # Bracketed placeholders are variables, not literal agent names.
        case "${value}" in
          \[*) continue ;;
        esac

        # Empty value (e.g. trailing `subagent_type=` with nothing) — skip.
        [ -n "${value}" ] || continue

        if is_builtin "${value}"; then
          continue
        fi

        if ! is_known_agent "${value}"; then
          rel_file="${md_file#"${repo_root}/"}"
          echo "unresolved: ${rel_file}:${line_no}: subagent_type='${value}' has no matching file in agents/" >&2
          unresolved=$((unresolved + 1))
        fi
      done < <(grep -oE 'subagent_type=("[^"]*"|'\''[^'\'']*'\''|\[[^]]*\]|[A-Za-z0-9_.-]+)' <<<"${line}")
    done < "${md_file}"
  done < <(find "${search_dir}" -type f -name '*.md' | sort)
done

if [ "${unresolved}" -gt 0 ]; then
  echo "" >&2
  echo "FAIL: ${unresolved} unresolved subagent_type reference(s)." >&2
  echo "Add the missing agent under agents/<name>.md, or fix the reference." >&2
  exit 1
fi

echo "OK: all subagent_type references resolve to an agent file or a built-in type."
