#!/usr/bin/env bash
#
# bump-controller-version.sh — align the sdlc controller package version with
# a release tag (Issue #46).
#
# The release workflow (.github/workflows/release.yml) bumps the plugin and
# marketplace manifests on every tag, but historically left
# controller/pyproject.toml (and its uv.lock pin) stale — so `sdlc --version`
# reported an old number (e.g. 1.16.0) while the repo advanced. This helper
# sets both in lockstep with the tag. The release job does NOT set up Python /
# uv, so the edits are pure text (sed), not `uv version` / `uv lock`.
#
# Input:
#   $1   target version, with or without a leading `v` (e.g. v1.49.6 or
#        1.49.6). The leading `v` is stripped (PEP 440 / semver convention).
#   $2   path to pyproject.toml.
#   $3   path to uv.lock.
#
# Behaviour:
#   * pyproject.toml — rewrites ONLY the first `version = "..."` line that
#     follows the `[project]` table header (the project version), leaving any
#     other table's `version` key untouched.
#   * uv.lock — rewrites ONLY the `version = "..."` line inside the
#     `[[package]]` block whose `name = "sdlc-controller"`. Every other
#     package's version, and the top-of-file lock-format `version = 1`, is left
#     untouched.
#
# Output: a confirmation line per file (mirrors the workflow's manifest step).
#
# Exit status: non-zero if either file is missing or the expected anchor is not
# found, so a silent no-op can never masquerade as a successful bump.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <version> <pyproject.toml> <uv.lock>" >&2
    exit 2
fi

version="$1"
pyproject="$2"
uvlock="$3"
number="${version#v}"

for f in "${pyproject}" "${uvlock}"; do
    if [ ! -f "${f}" ]; then
        echo "error: file not found: ${f}" >&2
        exit 1
    fi
done

# --- pyproject.toml: the project version --------------------------------------
# Scoped with awk so only the first `version =` after the `[project]` header is
# rewritten. A plain `s/^version =/.../` would also hit any later table that
# happens to carry a `version` key.
tmp="$(mktemp)"
awk -v ver="${number}" '
    /^\[/        { in_project = ($0 == "[project]") }
    in_project && !done && /^version[[:space:]]*=/ {
        print "version = \"" ver "\""
        done = 1
        next
    }
    { print }
    END { if (!done) exit 3 }
' "${pyproject}" > "${tmp}" || {
    rm -f "${tmp}"
    echo "error: no [project] version line in ${pyproject}" >&2
    exit 1
}
mv "${tmp}" "${pyproject}"

# --- uv.lock: the sdlc-controller package pin ---------------------------------
# Rewrite the `version =` line only inside the [[package]] block whose
# `name = "sdlc-controller"`. The block ends at the next [[package]] / table
# header, so the scope cannot bleed into a neighbouring package, and the
# top-of-file `version = 1` (lock format) sits before any [[package]] and is
# therefore never matched.
tmp="$(mktemp)"
awk -v ver="${number}" '
    /^\[\[package\]\]/ { in_pkg = 1; is_target = 0; print; next }
    /^\[/ && !/^\[\[package\]\]/ { in_pkg = 0; is_target = 0 }
    in_pkg && /^name[[:space:]]*=[[:space:]]*"sdlc-controller"/ { is_target = 1 }
    in_pkg && is_target && !done && /^version[[:space:]]*=/ {
        print "version = \"" ver "\""
        done = 1
        next
    }
    { print }
    END { if (!done) exit 3 }
' "${uvlock}" > "${tmp}" || {
    rm -f "${tmp}"
    echo "error: no sdlc-controller package version in ${uvlock}" >&2
    exit 1
}
mv "${tmp}" "${uvlock}"

echo "Controller version set to ${number}:"
echo "  ${pyproject}: $(awk '/^\[project\]/{p=1} p&&/^version[[:space:]]*=/{print;exit}' "${pyproject}")"
echo "  ${uvlock}: sdlc-controller -> ${number}"
