#!/usr/bin/env bash
#
# overengineering-lens.sh — Codex reference implementation of the
# over-engineering review lens (Story 18.2-001, wired by issue #445).
#
# This is the default `command` in
# controller/src/sdlc/config/overengineering-lens.yaml. The controller's
# _dispatch_overengineering_advisory invokes it (advisory-only, after a
# successful review stage) when the lens is enabled; it can also be run
# manually against any PR. The wrapper:
#
#   1. Fetches the change-request diff via the host CLI — `gh pr diff` on
#      GitHub, `glab mr diff` on GitLab. The host comes from --host /
#      LENS_HOST, else is auto-detected from the `origin` remote, and
#      defaults to GitHub.
#   2. Invokes a Codex delete-list pass via `codex exec`, instructing it to
#      end with a single fenced ```json block conforming to the
#      over-engineering lens response contract.
#   3. Extracts that JSON block, normalises it (defaults summary/findings,
#      coerces unknown categories to "other"), and prints it to stdout.
#
# The emitted JSON validates against
# controller/src/sdlc/schemas/overengineering-lens-response.schema.json so the
# controller's parse_lens_response() accepts it unchanged.
#
# Usage:
#   overengineering-lens.sh --pr-number <N> [--host github|gitlab]
#
# Options:
#   --pr-number N   Change request to review (required; a positive integer).
#                   The GitHub PR number or the GitLab MR IID.
#   --host H        Code host: github (default) or gitlab. Selects the diff
#                   source — `gh pr diff` vs `glab mr diff`. When omitted,
#                   auto-detected from the `origin` remote, falling back to
#                   github. Override the default via LENS_HOST.
#   -h, --help      Show this help.
#
# Environment (testing seams — not for normal use):
#   LENS_RAW_OUTPUT  Path to a captured Codex transcript. When set, the wrapper
#                    skips the host CLI/`codex` and parses this file. CI uses it
#                    so no real lens subprocess runs.
#   LENS_HOST        Default host (github|gitlab) when --host is omitted and no
#                    `origin` remote resolves.
#
# Exit status:
#   0  emitted a valid lens delete-list on stdout
#   1  could not produce a delete-list (no JSON in transcript, bad shape, etc.)
#   2  usage / environment error

set -euo pipefail

VALID_HOSTS=("github" "gitlab")
VALID_CATEGORIES='["speculative_abstraction","unused_code","reinvented_wheel","premature_generality","other"]'

# detect_host — map the `origin` remote to github/gitlab by hostname, mirroring
# the controller's issue_host.host_from_remote heuristic. Prints github|gitlab,
# or nothing when the remote is absent/unrecognised (caller then defaults).
detect_host() {
  local remote host
  remote="$(git remote get-url origin 2>/dev/null)" || return 0
  # Match the HOST portion only, never the repo path — a GitHub repo whose
  # owner/name contains "gitlab" must route through `gh`, not `glab`.
  host="${remote#*://}"   # drop scheme:// (https form)
  host="${host#*@}"       # drop user@ (scp form)
  host="${host%%/*}"      # drop /path (https form)
  host="${host%%:*}"      # drop :path / :port (scp form)
  case "${host}" in
    *gitlab*) printf 'gitlab' ;;
    *github*) printf 'github' ;;
    *) : ;;
  esac
}

# die <message> [exit_code]; defaults to exit 2 (usage/environment error).
die() {
  local code=2
  if [ "$#" -ge 2 ]; then
    code="${*: -1}"
    set -- "${@:1:$#-1}"
  fi
  echo "error: $*" >&2
  exit "${code}"
}

usage() {
  sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p;}' "$0"
}

# --- Parse arguments -------------------------------------------------------
pr_number=""
host=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pr-number)
      pr_number="${2:-}"
      shift 2 || die "--pr-number requires a value"
      ;;
    --pr-number=*)
      pr_number="${1#*=}"
      shift
      ;;
    --host)
      host="${2:-}"
      shift 2 || die "--host requires a value"
      ;;
    --host=*)
      host="${1#*=}"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

# --- Validate inputs -------------------------------------------------------
if [ -z "${pr_number}" ]; then
  die "--pr-number is required"
fi
case "${pr_number}" in
  '' | *[!0-9]*) die "--pr-number must be a positive integer (got: ${pr_number})" ;;
  0) die "--pr-number must be a positive integer (got: ${pr_number})" ;;
esac

# Resolve the host: explicit --host wins, else auto-detect from the remote,
# else the LENS_HOST default, else github.
if [ -z "${host}" ]; then
  host="$(detect_host)"
fi
if [ -z "${host}" ]; then
  host="${LENS_HOST:-github}"
fi
host_ok=0
for h in "${VALID_HOSTS[@]}"; do
  [ "${host}" = "${h}" ] && host_ok=1 && break
done
if [ "${host_ok}" -eq 0 ]; then
  die "--host must be one of: ${VALID_HOSTS[*]} (got: ${host})"
fi

# --- Obtain the lens transcript --------------------------------------------
# The test seam short-circuits the real lens so CI is hermetic.
if [ -n "${LENS_RAW_OUTPUT:-}" ]; then
  [ -f "${LENS_RAW_OUTPUT}" ] || die "LENS_RAW_OUTPUT not found: ${LENS_RAW_OUTPUT}"
  transcript="$(cat "${LENS_RAW_OUTPUT}")"
else
  if [ "${host}" = "gitlab" ]; then
    host_cli="glab"
    cr_args=("mr" "diff" "${pr_number}")
  else
    host_cli="gh"
    cr_args=("pr" "diff" "${pr_number}")
  fi

  command -v "${host_cli}" >/dev/null 2>&1 \
    || die "${host_cli} CLI not found; cannot fetch change request #${pr_number} on ${host}"
  command -v codex >/dev/null 2>&1 || die "codex CLI not found; cannot run the lens pass"

  diff="$("${host_cli}" "${cr_args[@]}" 2>/dev/null)" \
    || die "failed to fetch diff for change request #${pr_number} via ${host_cli}" 1

  prompt="Review pull request #${pr_number} for over-engineering ONLY: produce a
structured delete-list of over-built code in the diff below.

Diff under review:
${diff}

Look exclusively for code that should be DELETED or simplified:
- speculative_abstraction: interfaces/hooks/layers built for callers that do not exist
- unused_code: parameters, branches, or config nothing exercises
- reinvented_wheel: hand-rolled code a stdlib, existing dep, or one-liner covers
- premature_generality: configurability or genericity beyond the story's need
Would a senior engineer call the diff over-complicated? If the diff is already
minimal, say so and return an empty findings list. Do not report bugs, style,
or security issues — other gates own those.

After your review, output ONLY a single fenced json block conforming to the
over-engineering lens response contract, with keys: summary (one paragraph;
says so explicitly when the diff is already minimal) and findings (array of
{category, file, line, reason}; category one of speculative_abstraction,
unused_code, reinvented_wheel, premature_generality, other; line may be null
for file-level findings; reason is a one-line 'why' naming what to delete)."

  transcript="$(codex exec "${prompt}" 2>/dev/null)" \
    || die "codex exec failed running the lens pass" 1
fi

# --- Extract the lens JSON block -------------------------------------------
# The lens is instructed to end with a fenced ```json block. Pull out the last
# such block (awk state machine) so leading prose never confuses the parser.
json_block="$(
  printf '%s\n' "${transcript}" | awk '
    /^```[Jj][Ss][Oo][Nn][[:space:]]*$/ { capture=1; buf=""; next }
    /^```[[:space:]]*$/ {
      if (capture) { last=buf; capture=0 }
      next
    }
    capture { buf = buf $0 "\n" }
    END { printf "%s", last }
  '
)"

if [ -z "${json_block}" ]; then
  die "no lens JSON block found in the transcript for PR #${pr_number}" 1
fi

# Validate it parses as a JSON object before we normalise it.
if ! printf '%s' "${json_block}" | jq -e 'type == "object"' >/dev/null 2>&1; then
  die "no lens JSON object could be parsed from the transcript" 1
fi

# Findings must be an array (or absent) — anything else is a contract miss.
if ! printf '%s' "${json_block}" | jq -e '(.findings // []) | type == "array"' >/dev/null 2>&1; then
  die "lens findings must be an array" 1
fi

# Normalise:
#   - default summary/findings so the schema's required set is always present
#   - coerce unknown categories to "other" (schema enum), keep line/file/reason
#   - drop findings with no file or no reason (schema requires both non-empty)
printf '%s' "${json_block}" | jq \
  --argjson valid "${VALID_CATEGORIES}" \
  '{
     summary: (.summary // ""),
     findings: [
       (.findings // [])[]
       | select((.file // "") != "" and (.reason // "") != "")
       | {
           category: (if ((.category // "") as $c | $valid | index($c)) then .category else "other" end),
           file: .file,
           line: (.line // null),
           reason: .reason
         }
     ]
   }'
