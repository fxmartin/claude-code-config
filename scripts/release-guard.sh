#!/usr/bin/env bash
# release-guard.sh — decide whether the release workflow should proceed.
#
# Story 5.3-001. Reads a commit message on stdin (the head commit of the push
# that triggered the release workflow) and prints `proceed=true` or
# `proceed=false`.
#
# The decision keys ONLY off the commit SUBJECT (first line). The body is
# deliberately ignored: a PR body that documents the skip-release token, or a
# feature commit whose body quotes `chore(release):`, must still release.
# Matching the whole message here is the bug this script exists to prevent.
#
# Skip when the subject:
#   * starts with `chore(release):`  — the workflow's own bump commit (recursion)
#   * contains `[skip release]`      — the manual emergency escape hatch
set -euo pipefail

# Read the full commit message from stdin, then keep only the first line.
message="$(cat)"
subject="${message%%$'\n'*}"

proceed=true

case "${subject}" in
  "chore(release):"*)
    echo "Head commit subject is a release bump — skipping to avoid recursion." >&2
    proceed=false
    ;;
esac

case "${subject}" in
  *"[skip release]"*)
    echo "Head commit subject requests skip-release — skipping." >&2
    proceed=false
    ;;
esac

echo "proceed=${proceed}"
