#!/usr/bin/env bats
# Tests for scripts/codex-adversarial-review.sh (Story 8.1-002).
#
# The wrapper is the Codex reference implementation of the adversarial reviewer
# slot defined in Story 8.1-001. It fetches a PR, invokes a Codex review skill
# (`roast` or `project-review`), and emits the JSON shape the controller's
# `parse_reviewer_response()` validates against
# `adversarial-reviewer-response.schema.json`.
#
# Strategy: drive the wrapper through its test seam (CODEX_ADV_RAW_OUTPUT points
# at a captured Codex skill transcript) so no real `codex`/`gh` runs in CI, then
# assert the emitted JSON's shape, verdict, and findings. A companion controller
# pytest asserts schema-validity end to end.

WRAPPER="${BATS_TEST_DIRNAME}/../scripts/codex-adversarial-review.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/codex-adversarial"

@test "requires --pr-number" {
    run bash "${WRAPPER}"
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"--pr-number"* ]]
}

@test "rejects an unknown reviewer skill" {
    run bash "${WRAPPER}" --pr-number 42 --reviewer-skill nope
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"reviewer-skill"* ]]
}

@test "emits a structured approve verdict from a roast transcript" {
    CODEX_ADV_RAW_OUTPUT="${FIXTURES}/roast-approve.txt" \
        run bash "${WRAPPER}" --pr-number 42 --reviewer-skill roast
    [ "${status}" -eq 0 ]
    # Valid JSON object.
    echo "${output}" | jq -e . > /dev/null
    [ "$(echo "${output}" | jq -r .reviewer_name)" = "codex" ]
    [ "$(echo "${output}" | jq -r .verdict)" = "approve" ]
    [ "$(echo "${output}" | jq -r '.findings | type')" = "array" ]
}

@test "emits request_changes with findings from a roast transcript" {
    CODEX_ADV_RAW_OUTPUT="${FIXTURES}/roast-request-changes.txt" \
        run bash "${WRAPPER}" --pr-number 7
    [ "${status}" -eq 0 ]
    echo "${output}" | jq -e . > /dev/null
    [ "$(echo "${output}" | jq -r .verdict)" = "request_changes" ]
    [ "$(echo "${output}" | jq -r '.findings | length')" -ge 1 ]
    # Each finding carries the contract's required keys.
    [ "$(echo "${output}" | jq -r '.findings[0].severity')" = "error" ]
    [ "$(echo "${output}" | jq -r '.findings[0].category')" != "null" ]
    [ "$(echo "${output}" | jq -r '.findings[0].file')" != "null" ]
    [ "$(echo "${output}" | jq -r '.findings[0].message')" != "null" ]
}

@test "emits a block verdict and reports the reviewer skill used" {
    CODEX_ADV_RAW_OUTPUT="${FIXTURES}/roast-block.txt" \
        run bash "${WRAPPER}" --pr-number 9 --reviewer-skill project-review
    [ "${status}" -eq 0 ]
    [ "$(echo "${output}" | jq -r .verdict)" = "block" ]
    [ "$(echo "${output}" | jq -r .reviewer_skill)" = "project-review" ]
}

@test "fails closed when the transcript has no parseable verdict" {
    printf 'Some prose with no JSON verdict block at all.\n' > "${BATS_TMPDIR}/garbage.txt"
    CODEX_ADV_RAW_OUTPUT="${BATS_TMPDIR}/garbage.txt" \
        run bash "${WRAPPER}" --pr-number 1
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"no reviewer JSON"* ]]
}
