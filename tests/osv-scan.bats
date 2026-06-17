#!/usr/bin/env bats
# Tests for scripts/osv-scan.sh and the `sdlc depscan` classifier (Story 9.1-002).
#
# Strategy: osv-scanner is not installed in CI, so the gate's value is the
# classification step. We feed pre-captured osv-scanner --format=json reports
# (matching a real scan of a vulnerable lockfile) through `osv-scan.sh --report`
# and assert the DEP_SCAN_STATUS verdict and exit code. The headline AC — a
# fixture project with a known-vulnerable lockfile triggers BLOCK — is the first
# test.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
SCANNER="${REPO_ROOT}/scripts/osv-scan.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/osv"

setup() {
  # Drive the scanner's classify step through the controller in this repo.
  export SDLC_BIN="uv run --project ${REPO_ROOT}/controller sdlc"
}

@test "known-vulnerable lockfile report triggers BLOCK" {
  run bash "${SCANNER}" --report "${FIXTURES}/vulnerable-lockfile-report.json"
  [ "${status}" -eq 1 ]
  [[ "${output}" == *"DEP_SCAN_STATUS: BLOCK"* ]]
  [[ "${output}" == *"requests@2.19.0"* ]]
  [[ "${output}" == *"GHSA-9wx4-h78v-vm56"* ]]
}

@test "clean report yields CLEAN and exit 0" {
  run bash "${SCANNER}" --report "${FIXTURES}/clean-report.json"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"DEP_SCAN_STATUS: CLEAN"* ]]
}

@test "low-severity report yields WARN and exit 0" {
  run bash "${SCANNER}" --report "${FIXTURES}/low-severity-report.json"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"DEP_SCAN_STATUS: WARN"* ]]
}

@test "missing report file fails with usage error" {
  run bash "${SCANNER}" --report "${FIXTURES}/does-not-exist.json"
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"requires an existing report file"* ]]
}

@test "a per-repo suppression downgrades a BLOCK to CLEAN" {
  tmp_root="$(mktemp -d)"
  cp "${FIXTURES}/vulnerable-lockfile-report.json" "${tmp_root}/report.json"
  cat > "${tmp_root}/.dep-scan-suppressions.yaml" <<'YAML'
suppress:
  - id: GHSA-9wx4-h78v-vm56
    reason: not reachable from our proxy config; suppressed for this test
    expires: 2999-01-01
YAML
  run env SDLC_BIN="${SDLC_BIN}" \
    bash -c "uv run --project '${REPO_ROOT}/controller' sdlc depscan --suppressions '${tmp_root}/.dep-scan-suppressions.yaml' '${tmp_root}/report.json'"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"DEP_SCAN_STATUS: CLEAN"* ]]
  [[ "${output}" == *"[suppressed]"* ]]
  rm -rf "${tmp_root}"
}

@test "an expired suppression fails the gate with exit 2" {
  tmp_root="$(mktemp -d)"
  cp "${FIXTURES}/vulnerable-lockfile-report.json" "${tmp_root}/report.json"
  cat > "${tmp_root}/.dep-scan-suppressions.yaml" <<'YAML'
suppress:
  - id: GHSA-9wx4-h78v-vm56
    reason: deferred past its review date
    expires: 2000-01-01
YAML
  run env SDLC_BIN="${SDLC_BIN}" \
    bash -c "uv run --project '${REPO_ROOT}/controller' sdlc depscan --suppressions '${tmp_root}/.dep-scan-suppressions.yaml' '${tmp_root}/report.json'"
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"expired"* ]]
  rm -rf "${tmp_root}"
}
