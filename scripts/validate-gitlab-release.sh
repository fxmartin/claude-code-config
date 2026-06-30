#!/usr/bin/env bash
#
# validate-gitlab-release.sh — GitLab CI release-template validator (Story 23.4-001).
#
# Validates the installable GitLab CI release pipeline shipped for repos adopting
# the autonomous-SDLC build loop on GitLab. It is the GitLab port of Epic-05's
# GitHub-Actions release flow (.github/workflows/release.yml): on a push to the
# default branch it computes the Conventional-Commit semver bump, creates a
# `vX.Y.Z` tag, and publishes a GitLab Release with generated notes.
#
# It checks that the template:
#   1. parses as valid YAML whose top level is a mapping,
#   2. declares pipeline `stages` including a `release` stage,
#   3. declares the `publish-release` job,
#   4. reuses Epic-05's bump logic — references `scripts/compute-release.sh`
#      rather than re-implementing the bumper (port, don't fork),
#   5. publishes via `release-cli` (GitLab Releases, Free/Core tier),
#   6. is scoped to the default branch (`$CI_DEFAULT_BRANCH`) and never triggers
#      on a merge-request pipeline — the GitLab equivalent of GitHub's
#      `on: push: branches: [main]`,
#   7. carries the loop guard — skips its own `chore(release):` bump commit so a
#      release never retriggers a release (same semantics as release-guard.sh),
#   8. uses only GitLab Free/Core constructs — no Premium/Ultimate keywords.
#
# This is a static, read-only check: it does not run the pipeline. It mirrors
# scripts/validate-gitlab-ci.sh so the two installable templates are validated
# the same way in CI.
#
# YAML parsing uses `uv run --with pyyaml` so no system PyYAML is required;
# falls back to a system `python3` that already has PyYAML if uv is absent.
#
# Exit status:
#   0  template is valid
#   1  validation failed (missing job, bad YAML, wrong scope, Premium keyword, ...)
#   2  usage / environment error
#
# Usage:
#   scripts/validate-gitlab-release.sh [TEMPLATE_PATH]
#
# TEMPLATE_PATH defaults to templates/gitlab-ci-release.yml relative to the repo root.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

template="${1:-${repo_root}/templates/gitlab-ci-release.yml}"

if [ ! -f "${template}" ]; then
  echo "error: template not found: ${template}" >&2
  exit 1
fi

# Top-level keys (jobs/keywords) the template must declare.
required_keys=(
  stages          # pipeline stages
  publish-release # the release job
)

# Required source strings — each enforces an acceptance criterion that a
# top-level-key check cannot express on its own.
required_strings=(
  compute-release.sh # reuse Epic-05 bump logic (port, don't fork)
  release-cli        # publish a GitLab Release on Free/Core
  CI_DEFAULT_BRANCH  # scoped to the default branch
)

# Premium/Ultimate-only constructs that must never appear (Free/Core only).
forbidden_keywords=(
  merge_train
)

# --- Pick a Python interpreter that can import PyYAML ----------------------
py_run() {
  # Run python source ("$1") with PyYAML available; remaining args become
  # sys.argv[1:] for the script.
  local src="$1"
  shift
  if command -v uv >/dev/null 2>&1; then
    uv run --no-project --with pyyaml --quiet python3 -c "${src}" "$@"
  elif python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 -c "${src}" "$@"
  else
    echo "error: need uv or a python3 with PyYAML to parse YAML" >&2
    exit 2
  fi
}

# --- 1. Parse YAML and capture the top-level job/keyword set ---------------
parse_src="$(cat <<'PY'
import sys, yaml
path = sys.argv[1]
try:
    with open(path) as fh:
        doc = yaml.safe_load(fh)
except Exception as exc:  # noqa: BLE001 - surface any parse error verbatim
    print(f"PARSE_ERROR: {exc}", file=sys.stderr)
    sys.exit(3)
if not isinstance(doc, dict):
    print("PARSE_ERROR: top level is not a mapping", file=sys.stderr)
    sys.exit(3)
for key in doc:
    print(key)
PY
)"

if ! keys="$(py_run "${parse_src}" "${template}" 2>parse_err)"; then
  echo "error: ${template} is not valid YAML" >&2
  sed 's/^/  /' parse_err >&2 || true
  rm -f parse_err
  exit 1
fi
rm -f parse_err

# --- 2/3. Require every top-level job/keyword -----------------------------
for key in "${required_keys[@]}"; do
  if ! printf '%s\n' "${keys}" | grep -qx "${key}"; then
    echo "error: ${template} does not declare required key '${key}'" >&2
    exit 1
  fi
done

# The `release` stage must be listed under `stages:` (not merely a job name).
if ! grep -qE '^[[:space:]]*-[[:space:]]*release[[:space:]]*$' "${template}"; then
  echo "error: ${template} does not declare a 'release' stage" >&2
  exit 1
fi

# --- 4/5/6. Require the acceptance-criterion source strings ----------------
for needle in "${required_strings[@]}"; do
  if ! grep -q "${needle}" "${template}"; then
    case "${needle}" in
      compute-release.sh)
        echo "error: ${template} must reuse Epic-05's compute-release.sh (port, don't fork)" >&2
        ;;
      release-cli)
        echo "error: ${template} must publish via release-cli (GitLab Releases, Free/Core)" >&2
        ;;
      CI_DEFAULT_BRANCH)
        echo "error: ${template} must scope the release job to the default branch (\$CI_DEFAULT_BRANCH)" >&2
        ;;
      *)
        echo "error: ${template} is missing required content: ${needle}" >&2
        ;;
    esac
    exit 1
  fi
done

# The release flow must never run on a merge-request pipeline.
if grep -q 'merge_request_event' "${template}"; then
  echo "error: ${template} must not run the release job on merge requests (default branch only)" >&2
  exit 1
fi

# --- 7. Require the chore(release) loop guard ------------------------------
# A release must not retrigger a release. The guard keys off the commit subject
# (CI_COMMIT_TITLE) matching `chore(release):` and skipping — same semantics as
# release-guard.sh. We assert the chore(release) token is present as a guard.
if ! grep -qE 'chore\\?\(release\\?\)' "${template}"; then
  echo "error: ${template} is missing the chore(release) loop guard (would re-release its own bump)" >&2
  exit 1
fi

# --- 8. Reject Premium/Ultimate-only constructs ----------------------------
for kw in "${forbidden_keywords[@]}"; do
  if grep -iq "${kw}" "${template}"; then
    echo "error: ${template} uses a Premium/Ultimate-only construct: ${kw}" >&2
    echo "       the template must stay GitLab Free/Core" >&2
    exit 1
  fi
done

echo "OK: ${template} declares the GitLab release flow (Free/Core)"
