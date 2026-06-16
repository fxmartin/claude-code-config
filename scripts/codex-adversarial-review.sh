#!/usr/bin/env bash
#
# codex-adversarial-review.sh — Codex reference implementation of the
# adversarial reviewer slot (Story 8.1-002, Epic-08).
#
# This is the first concrete plug-in for the vendor-agnostic reviewer slot
# defined in Story 8.1-001. The controller registers it in
# controller/config/adversarial-reviewers.yaml as the `codex` reviewer and
# invokes it with `--pr-number {pr_number}`. The wrapper:
#
#   1. Fetches the PR diff via `gh pr diff`.
#   2. Invokes a Codex review skill (`roast` or `project-review`) via
#      `codex exec`, instructing it to end with a single fenced ```json block
#      that conforms to the adversarial-reviewer-response schema.
#   3. Extracts that JSON block, normalises it (forces reviewer_name=codex,
#      records which skill ran), and prints it to stdout.
#
# The emitted JSON validates against
# controller/src/sdlc/schemas/adversarial-reviewer-response.schema.json so the
# controller's parse_reviewer_response() accepts it unchanged.
#
# Usage:
#   codex-adversarial-review.sh --pr-number <N> [--reviewer-skill roast|project-review]
#
# Options:
#   --pr-number N         PR to review (required; must be a positive integer).
#   --reviewer-skill S    Codex review skill: roast (default) or project-review.
#                         Override per repo via CODEX_ADV_REVIEW_SKILL.
#   -h, --help            Show this help.
#
# Environment (testing seams — not for normal use):
#   CODEX_ADV_RAW_OUTPUT  Path to a captured Codex transcript. When set, the
#                         wrapper skips `gh`/`codex` and parses this file. CI
#                         uses it so no real reviewer subprocess runs.
#   CODEX_ADV_REVIEW_SKILL  Default reviewer skill when --reviewer-skill is
#                         omitted.
#
# Exit status:
#   0  emitted a valid reviewer verdict on stdout
#   1  could not produce a verdict (no JSON in transcript, bad verdict, etc.)
#   2  usage / environment error

set -euo pipefail

REVIEWER_NAME="codex"
VALID_SKILLS=("roast" "project-review")
VALID_VERDICTS=("approve" "request_changes" "block")

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
reviewer_skill="${CODEX_ADV_REVIEW_SKILL:-roast}"

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
    --reviewer-skill)
      reviewer_skill="${2:-}"
      shift 2 || die "--reviewer-skill requires a value"
      ;;
    --reviewer-skill=*)
      reviewer_skill="${1#*=}"
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
  '' | *[!0-9]*)
    die "--pr-number must be a positive integer, got: ${pr_number}"
    ;;
esac

skill_ok=0
for s in "${VALID_SKILLS[@]}"; do
  [ "${reviewer_skill}" = "${s}" ] && skill_ok=1 && break
done
if [ "${skill_ok}" -eq 0 ]; then
  die "--reviewer-skill must be one of: ${VALID_SKILLS[*]} (got: ${reviewer_skill})"
fi

# --- Obtain the Codex review transcript ------------------------------------
# The test seam short-circuits the real reviewer so CI is hermetic.
if [ -n "${CODEX_ADV_RAW_OUTPUT:-}" ]; then
  [ -f "${CODEX_ADV_RAW_OUTPUT}" ] || die "CODEX_ADV_RAW_OUTPUT not found: ${CODEX_ADV_RAW_OUTPUT}"
  transcript="$(cat "${CODEX_ADV_RAW_OUTPUT}")"
else
  command -v gh >/dev/null 2>&1 || die "gh CLI not found; cannot fetch PR #${pr_number}"
  command -v codex >/dev/null 2>&1 || die "codex CLI not found; cannot run the ${reviewer_skill} skill"

  diff="$(gh pr diff "${pr_number}" 2>/dev/null)" \
    || die "failed to fetch diff for PR #${pr_number} via gh" 1

  prompt="Use ${reviewer_skill} to review pull request #${pr_number}.

Diff under review:
${diff}

After your review, output ONLY a single fenced json block conforming to the
adversarial reviewer response contract, with keys: reviewer_name (\"codex\"),
verdict (one of approve, request_changes, block), summary (one paragraph), and
findings (array of {severity, category, file, line, message}; line may be null).
Choose 'block' for a critical/security regression, 'request_changes' for
fixable issues, and 'approve' when nothing blocks merge."

  transcript="$(codex exec "${prompt}" 2>/dev/null)" \
    || die "codex exec failed running the ${reviewer_skill} skill" 1
fi

# --- Extract the reviewer JSON block ---------------------------------------
# The skill is instructed to end with a fenced ```json block. Pull out the last
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
  die "no reviewer JSON block found in the Codex transcript for PR #${pr_number}" 1
fi

# Validate it parses as a JSON object before we normalise it.
if ! printf '%s' "${json_block}" | jq -e 'type == "object"' >/dev/null 2>&1; then
  die "no reviewer JSON object could be parsed from the Codex transcript" 1
fi

# --- Validate the verdict and normalise ------------------------------------
verdict="$(printf '%s' "${json_block}" | jq -r '.verdict // empty')"
verdict_ok=0
for v in "${VALID_VERDICTS[@]}"; do
  [ "${verdict}" = "${v}" ] && verdict_ok=1 && break
done
if [ "${verdict_ok}" -eq 0 ]; then
  die "reviewer verdict must be one of: ${VALID_VERDICTS[*]} (got: ${verdict:-<missing>})" 1
fi

# Normalise:
#   - force reviewer_name to "codex" (matches the registry key)
#   - default summary/findings so the required set is always present
#   - record which skill produced the verdict (extra field; schema allows it)
printf '%s' "${json_block}" | jq \
  --arg name "${REVIEWER_NAME}" \
  --arg skill "${reviewer_skill}" \
  '{
     reviewer_name: $name,
     verdict: .verdict,
     summary: (.summary // ""),
     findings: (.findings // []),
     reviewer_skill: $skill
   }'
