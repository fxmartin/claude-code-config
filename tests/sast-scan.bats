#!/usr/bin/env bats
# Tests for scripts/sast-scan.sh and the `sdlc sast` classifier (Story 9.1-001).
#
# Strategy: semgrep is not installed in CI, so the gate's value is the
# classification step. We feed pre-captured semgrep --json reports (captured
# from a real scan of the fixtures) through `sast-scan.sh --report` and assert
# the SAST_STATUS verdict and exit code. The headline AC — a known-bad fixture
# (tests/fixtures/sql-injection.py) triggers BLOCK — is the first test.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
SCANNER="${REPO_ROOT}/scripts/sast-scan.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/sast"

setup() {
  # Drive the scanner's classify step through the controller in this repo.
  export SDLC_BIN="uv run --project ${REPO_ROOT}/controller sdlc"
}

@test "known-bad sql-injection fixture triggers BLOCK" {
  run bash "${SCANNER}" --report "${FIXTURES}/sql-injection-report.json"
  [ "${status}" -eq 1 ]
  [[ "${output}" == *"SAST_STATUS: BLOCK"* ]]
  [[ "${output}" == *"formatted-sql-query"* ]]
}

@test "clean report yields CLEAN and exit 0" {
  run bash "${SCANNER}" --report "${FIXTURES}/clean-report.json"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"SAST_STATUS: CLEAN"* ]]
}

@test "warning-only report yields WARN and exit 0" {
  run bash "${SCANNER}" --report "${FIXTURES}/warning-report.json"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"SAST_STATUS: WARN"* ]]
}

@test "missing report file fails with usage error" {
  run bash "${SCANNER}" --report "${FIXTURES}/does-not-exist.json"
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"requires an existing report file"* ]]
}

@test "a per-repo suppression downgrades a BLOCK to CLEAN" {
  tmp_root="$(mktemp -d)"
  cp "${FIXTURES}/sql-injection-report.json" "${tmp_root}/report.json"
  cat > "${tmp_root}/.sast-config.yaml" <<'YAML'
suppress:
  - id: python.lang.security.audit.formatted-sql-query.formatted-sql-query
    reason: fixture intentionally vulnerable; suppressed for this test
YAML
  run env SDLC_BIN="${SDLC_BIN}" \
    bash -c "cd '${tmp_root}' && uv run --project '${REPO_ROOT}/controller' sdlc sast --config '${tmp_root}/.sast-config.yaml' '${tmp_root}/report.json'"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"SAST_STATUS: CLEAN"* ]]
  [[ "${output}" == *"[suppressed]"* ]]
  rm -rf "${tmp_root}"
}
