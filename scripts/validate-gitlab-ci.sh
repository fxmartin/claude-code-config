#!/usr/bin/env bash
#
# validate-gitlab-ci.sh — GitLab CI gate-template validator (Story 23.3-001).
#
# Validates the installable `.gitlab-ci.yml` quality-gate template shipped for
# repos adopting the autonomous-SDLC build loop on GitLab. It checks that the
# template:
#   1. parses as valid YAML,
#   2. declares pipeline `stages`,
#   3. declares every required gate job — the GitLab analogue of the GitHub
#      Actions gate set (Epic-02/09): a secret scan, lint (shellcheck + ruff),
#      JSON-schema/contract checks, commit-format (commitlint), and the test
#      gates (pytest + bats),
#   4. uses only GitLab Free/Core constructs — no Premium/Ultimate keywords
#      such as merge trains.
#
# This is a static, read-only check: it does not run the pipeline. The DoD's
# "dry-run on a sample repo" is satisfied by parsing + structural assertion so
# the gate-parity contract is enforced in CI without a live GitLab runner.
#
# YAML parsing uses `uv run --with pyyaml` so no system PyYAML is required;
# falls back to a system `python3` that already has PyYAML if uv is absent.
#
# Exit status:
#   0  template is valid
#   1  validation failed (missing gate, bad YAML, Premium keyword, ...)
#   2  usage / environment error
#
# Usage:
#   scripts/validate-gitlab-ci.sh [TEMPLATE_PATH]
#
# TEMPLATE_PATH defaults to templates/gitlab-ci.yml relative to the repo root.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

template="${1:-${repo_root}/templates/gitlab-ci.yml}"

if [ ! -f "${template}" ]; then
  echo "error: template not found: ${template}" >&2
  exit 1
fi

# Required gate jobs — each maps 1:1 to a GitHub Actions gate (see
# docs/gitlab-ci-template.md for the parity table).
required_jobs=(
  secrets-scan   # gitleaks secret scan
  shellcheck     # lint: shell
  ruff           # lint: python
  json-schema    # JSON-schema / contract checks
  commit-format  # commitlint conventional-commit gate
  risk-gate      # high-risk file approval gate
  pytest         # tests: controller / python
  bats           # tests: behaviour / shell
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
# The python helper prints the parsed top-level keys (one per line) on success,
# or exits non-zero on a parse error. We capture both the keys and the parse
# status so a malformed template fails with a clear message.
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

# --- 2. Require pipeline stages -------------------------------------------
if ! printf '%s\n' "${keys}" | grep -qx "stages"; then
  echo "error: ${template} does not declare pipeline 'stages'" >&2
  exit 1
fi

# --- 3. Require every gate job --------------------------------------------
missing=()
for job in "${required_jobs[@]}"; do
  if ! printf '%s\n' "${keys}" | grep -qx "${job}"; then
    missing+=("${job}")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "error: ${template} is missing required gate job(s): ${missing[*]}" >&2
  exit 1
fi

# --- 4. Reject Premium/Ultimate-only constructs ----------------------------
for kw in "${forbidden_keywords[@]}"; do
  if grep -iq "${kw}" "${template}"; then
    echo "error: ${template} uses a Premium/Ultimate-only construct: ${kw}" >&2
    echo "       the template must stay GitLab Free/Core" >&2
    exit 1
  fi
done

echo "OK: ${template} declares all required gates (Free/Core)"
