#!/usr/bin/env bats
# Tests for scripts/run-type-check.sh and the `sdlc typecheck` ratchet (Issue #586).
#
# Two layers are covered. The fixture tests drive `--report` with hand-written
# mypy output so the ratchet's gating logic is asserted without running mypy.
# The last test is the headline AC end-to-end: the real gate is green on the
# committed baseline, and goes red when a type error is introduced.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
GATE="${REPO_ROOT}/scripts/run-type-check.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/type-check"

setup() {
  # Drive the ratchet through the controller in this repo, pinned to a fixture
  # baseline so these tests never depend on the real backlog's size.
  export SDLC_BIN="uv run --project ${REPO_ROOT}/controller sdlc"
}

# The gate always resolves the baseline next to controller/pyproject.toml, so
# fixture-baseline tests call the controller directly rather than via the gate.
ratchet() {
  local baseline="$1" report="$2"
  # shellcheck disable=SC2086
  ${SDLC_BIN} typecheck --baseline "${baseline}" "${report}"
}

@test "a known violation alone does not fail the build" {
  run ratchet "${FIXTURES}/baseline.json" "${FIXTURES}/known-only.txt"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: CLEAN"* ]]
}

@test "a newly added violation fails the build" {
  run ratchet "${FIXTURES}/baseline.json" "${FIXTURES}/known-plus-new.txt"
  [ "${status}" -eq 1 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: BLOCK"* ]]
  [[ "${output}" == *"src/sdlc/fresh.py:12"* ]]
  # The baselined violation must not be reported as new.
  [[ "${output}" != *"[new] src/sdlc/known.py"* ]]
}

@test "a known violation that moved to another line still passes" {
  run ratchet "${FIXTURES}/baseline.json" "${FIXTURES}/known-moved.txt"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: CLEAN"* ]]
}

@test "draining the backlog warns but passes" {
  run ratchet "${FIXTURES}/baseline.json" "${FIXTURES}/clean.txt"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: WARN"* ]]
  [[ "${output}" == *"--update"* ]]
}

@test "a malformed baseline is an environment error, not a pass" {
  run ratchet "${FIXTURES}/malformed-baseline.json" "${FIXTURES}/known-only.txt"
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"not valid JSON"* ]]
}

@test "missing report file fails with usage error" {
  run bash "${GATE}" --report "${FIXTURES}/does-not-exist.txt"
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"requires an existing report file"* ]]
}

@test "an unknown argument fails with usage error" {
  run bash "${GATE}" --nope
  [ "${status}" -eq 2 ]
  [[ "${output}" == *"unknown argument"* ]]
}

@test "the gate is green on the committed baseline and red on a real type error" {
  # End-to-end: runs mypy for real against controller/src.
  run bash "${GATE}"
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: CLEAN"* ]]

  # progress.py is on the strict allowlist; a bad return type must be caught.
  probe="${REPO_ROOT}/controller/src/sdlc/progress.py"
  backup="$(mktemp -t progress-backup.XXXXXX.py)"
  cp "${probe}" "${backup}"
  printf '\n\ndef _bats_type_probe() -> int:\n    return "not an int"\n' >> "${probe}"

  run bash "${GATE}"
  cp "${backup}" "${probe}"
  rm -f "${backup}"

  [ "${status}" -eq 1 ]
  [[ "${output}" == *"TYPE_CHECK_STATUS: BLOCK"* ]]
  [[ "${output}" == *"_bats_type_probe"* || "${output}" == *"Incompatible return value type"* ]]
}
