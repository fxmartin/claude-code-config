#!/usr/bin/env bash
#
# risk-gate-detect.sh — High-risk file pattern detector (Story 8.2-001).
#
# Reads a newline-delimited list of changed file paths on stdin and prints,
# one per line, each path that matches a high-risk glob pattern together with
# the pattern it hit ("PATH\tPATTERN"). Exit status reflects whether any match
# was found, so a CI gate can fail the check when the risk set is non-empty.
#
# Patterns are loaded from controller/config/high-risk-patterns.yaml (the
# baseline shared with the controller's sdlc.risk_gate module) plus, when
# present, an additive per-repo override at .sdlc-risk-config.yaml in REPO_ROOT.
#
# Glob semantics mirror the controller matcher and the config comments:
#   **  crosses path separators (a leading "**/" also matches at the root)
#   *   matches within a single path segment (never crosses "/")
#   ?   matches a single non-separator character
#
# Exit status:
#   0  at least one changed file matched a high-risk pattern
#   1  no changed file matched (the change set is clean)
#   2  usage / environment error
#
# Usage:
#   git diff --name-only origin/main... | scripts/risk-gate-detect.sh [REPO_ROOT]
#
# REPO_ROOT defaults to the git toplevel containing this script.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="${1:-}"
if [ -z "${repo_root}" ]; then
  repo_root="$(cd "${script_dir}/.." && pwd)"
fi

config_file="${RISK_GATE_CONFIG:-${repo_root}/controller/config/high-risk-patterns.yaml}"
override_file="${repo_root}/.sdlc-risk-config.yaml"

if [ ! -f "${config_file}" ]; then
  echo "error: high-risk config not found at ${config_file}" >&2
  exit 2
fi

# --- Load patterns ---------------------------------------------------------
# The config is a flat YAML list under `high_risk_patterns:`. Extract the
# quoted/unquoted scalar after each leading "- ", ignoring comments and blank
# lines. This intentionally avoids a YAML dependency so the gate runs anywhere
# bash + sed are available.
extract_patterns() {
  # $1: file to read
  sed -n 's/^[[:space:]]*-[[:space:]]*//p' "$1" \
    | sed 's/[[:space:]]*#.*$//' \
    | sed 's/^"\(.*\)"$/\1/; s/^'\''\(.*\)'\''$/\1/' \
    | sed 's/[[:space:]]*$//' \
    | grep -v '^$' || true
}

patterns=()
while IFS= read -r line; do
  patterns+=("${line}")
done < <(extract_patterns "${config_file}")

if [ -f "${override_file}" ]; then
  while IFS= read -r extra; do
    # Additive, de-duplicated: skip patterns already in the baseline.
    duplicate=0
    for existing in "${patterns[@]}"; do
      if [ "${existing}" = "${extra}" ]; then
        duplicate=1
        break
      fi
    done
    if [ "${duplicate}" -eq 0 ]; then
      patterns+=("${extra}")
    fi
  done < <(extract_patterns "${override_file}")
fi

if [ "${#patterns[@]}" -eq 0 ]; then
  echo "error: no high-risk patterns loaded from ${config_file}" >&2
  exit 2
fi

# --- Glob matching ---------------------------------------------------------
# Translate a gitignore-style glob into an anchored ERE, then match with grep.
glob_to_ere() {
  local glob="$1" out="" i=0 ch next
  local n=${#glob}

  # A leading "**/" also matches zero leading directories.
  if [ "${glob:0:3}" = "**/" ]; then
    out="(.*/)?"
    glob="${glob:3}"
    n=${#glob}
  fi

  while [ "${i}" -lt "${n}" ]; do
    ch="${glob:${i}:1}"
    case "${ch}" in
      '*')
        next="${glob:$((i + 1)):1}"
        if [ "${next}" = "*" ]; then
          out+=".*"
          i=$((i + 2))
          if [ "${glob:${i}:1}" = "/" ]; then
            i=$((i + 1))
          fi
        else
          out+="[^/]*"
          i=$((i + 1))
        fi
        ;;
      '?')
        out+="[^/]"
        i=$((i + 1))
        ;;
      '.' | '+' | '(' | ')' | '|' | '^' | '$' | '{' | '}' | '[' | ']' | '\\')
        out+="\\${ch}"
        i=$((i + 1))
        ;;
      *)
        out+="${ch}"
        i=$((i + 1))
        ;;
    esac
  done
  printf '^%s$' "${out}"
}

# Pre-compile each pattern to its ERE once.
eres=()
for p in "${patterns[@]}"; do
  eres+=("$(glob_to_ere "${p}")")
done

# --- Scan stdin ------------------------------------------------------------
matched_any=0
while IFS= read -r path; do
  [ -z "${path}" ] && continue
  idx=0
  for ere in "${eres[@]}"; do
    if printf '%s' "${path}" | grep -Eq "${ere}"; then
      printf '%s\t%s\n' "${path}" "${patterns[${idx}]}"
      matched_any=1
      break
    fi
    idx=$((idx + 1))
  done
done

if [ "${matched_any}" -eq 1 ]; then
  exit 0
fi
exit 1
