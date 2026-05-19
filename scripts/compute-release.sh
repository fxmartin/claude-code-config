#!/usr/bin/env bash
#
# compute-release.sh — Conventional Commit semver bumper (Story 5.2-001).
#
# Reads a stream of commit messages on stdin and computes the next semantic
# version from the current one. It is the single source of bump truth for the
# release workflow (.github/workflows/release.yml); pinning the logic in a
# shell script keeps it shellcheck-clean and unit-testable with bats.
#
# Input:
#   $1       current version, with or without a leading `v` (e.g. v1.3.0).
#   stdin    commit messages. Records may be NUL-separated (`git log -z`) or
#            plain text — the parser scans line by line and is agnostic to the
#            record boundary, so a collapsed/concatenated stream still works.
#
# Bump rules (Conventional Commits; highest wins):
#   MAJOR  any `BREAKING CHANGE:` footer, or a `!` before the `:` in a header.
#   MINOR  any `feat` header (if no MAJOR).
#   PATCH  any `fix`, `perf`, or `refactor` header (if no MAJOR/MINOR).
#   none   only chore/docs/test/ci/build (or no conventional commits) — the
#          workflow treats this as "no release" and exits 0.
#
# Output (stdout, two lines):
#   BUMP=<major|minor|patch|none>
#   VERSION=v<X.Y.Z>            # unchanged from the input when BUMP=none
#
# A "no release" diagnostic line is also printed when BUMP=none so a human
# reading the workflow log understands why nothing was tagged.
#
# Exit status:
#   0  computed successfully (including the no-release case)
#   2  usage / input error (missing or malformed current version)

set -euo pipefail

# --- Validate the current version -----------------------------------------
current="${1:-}"
if [ -z "${current}" ]; then
  echo "error: current version argument is required (e.g. v1.3.0)" >&2
  exit 2
fi

# Accept an optional leading `v`; require a strict MAJOR.MINOR.PATCH triple.
current="${current#v}"
if [[ ! "${current}" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "error: '${1}' is not a valid semantic version (expected vX.Y.Z)" >&2
  exit 2
fi
major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
patch="${BASH_REMATCH[3]}"

# --- Scan stdin for the highest-ranking Conventional Commit signal ---------
# rank: 0 none, 1 patch, 2 minor, 3 major.
rank=0

# Conventional Commit header: `type` optionally `(scope)`, optional `!`, `:`.
# We only need the type and whether a `!` precedes the colon.
header_re='^([a-z]+)(\([a-z0-9._/-]+\))?(!)?:'

while IFS= read -r line || [ -n "${line}" ]; do
  # MAJOR signal: a BREAKING CHANGE footer anywhere in the stream.
  if [[ "${line}" == "BREAKING CHANGE:"* || "${line}" == "BREAKING-CHANGE:"* ]]; then
    rank=3
    continue
  fi

  # Header line — classify by type and the breaking `!` marker.
  if [[ "${line}" =~ ${header_re} ]]; then
    type="${BASH_REMATCH[1]}"
    bang="${BASH_REMATCH[3]}"

    if [ -n "${bang}" ]; then
      rank=3
      continue
    fi

    case "${type}" in
      feat)
        [ "${rank}" -lt 2 ] && rank=2
        ;;
      fix | perf | refactor)
        [ "${rank}" -lt 1 ] && rank=1
        ;;
      *)
        # chore, docs, test, ci, build, revert, … — no release on their own.
        ;;
    esac
  fi
done

# --- Compute the new version ----------------------------------------------
case "${rank}" in
  3)
    bump="major"
    major=$((major + 1))
    minor=0
    patch=0
    ;;
  2)
    bump="minor"
    minor=$((minor + 1))
    patch=0
    ;;
  1)
    bump="patch"
    patch=$((patch + 1))
    ;;
  *)
    bump="none"
    ;;
esac

new_version="v${major}.${minor}.${patch}"

echo "BUMP=${bump}"
echo "VERSION=${new_version}"

if [ "${bump}" = "none" ]; then
  echo "no release: no feat/fix/perf/refactor or breaking commits since ${1}" >&2
fi
