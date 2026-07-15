#!/usr/bin/env bats
# Tests for scripts/overengineering-lens.sh (issue #445 follow-up).
#
# The wrapper is the default `command` in overengineering-lens.yaml — the
# runtime the controller's _dispatch_overengineering_advisory invokes when the
# lens is enabled. It fetches a PR diff, runs a Codex delete-list pass, and
# emits JSON conforming to overengineering-lens-response.schema.json so the
# controller's parse_lens_response() accepts it unchanged.
#
# Strategy: drive the wrapper through its test seam (LENS_RAW_OUTPUT points at
# a captured transcript) so no real `codex`/`gh` runs in CI, then assert the
# emitted JSON's shape and normalisation rules.

WRAPPER="${BATS_TEST_DIRNAME}/../scripts/overengineering-lens.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/overengineering-lens"

@test "requires --pr-number" {
    run bash "${WRAPPER}"
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"--pr-number"* ]]
}

@test "rejects a non-integer pr-number" {
    run bash "${WRAPPER}" --pr-number abc
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"positive integer"* ]]
}

@test "rejects an unknown host" {
    run bash "${WRAPPER}" --pr-number 42 --host sourcehut
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"--host"* ]]
}

@test "--help prints usage and exits 0" {
    run bash "${WRAPPER}" --help
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"--pr-number"* ]]
}

@test "emits an empty delete-list from a clean transcript" {
    LENS_RAW_OUTPUT="${FIXTURES}/lens-clean.txt" \
        run bash "${WRAPPER}" --pr-number 42
    [ "${status}" -eq 0 ]
    echo "${output}" | jq -e . > /dev/null
    [ "$(echo "${output}" | jq -r '.findings | length')" -eq 0 ]
    [ "$(echo "${output}" | jq -r '.summary')" != "" ]
}

@test "emits findings and takes the LAST fenced json block" {
    LENS_RAW_OUTPUT="${FIXTURES}/lens-findings.txt" \
        run bash "${WRAPPER}" --pr-number 7
    [ "${status}" -eq 0 ]
    echo "${output}" | jq -e . > /dev/null
    # The interim {"note": ...} block was skipped in favour of the final one.
    [ "$(echo "${output}" | jq -r 'has("note")')" = "false" ]
    [ "$(echo "${output}" | jq -r '.findings | length')" -eq 2 ]
    # Contract keys present on each finding.
    [ "$(echo "${output}" | jq -r '.findings[0].category')" = "unused_code" ]
    [ "$(echo "${output}" | jq -r '.findings[0].file')" = "controller/src/sdlc/build.py" ]
    [ "$(echo "${output}" | jq -r '.findings[0].line')" = "42" ]
    [ "$(echo "${output}" | jq -r '.findings[0].reason')" != "null" ]
}

@test "coerces an unknown category to other and keeps null line" {
    LENS_RAW_OUTPUT="${FIXTURES}/lens-findings.txt" \
        run bash "${WRAPPER}" --pr-number 7
    [ "${status}" -eq 0 ]
    [ "$(echo "${output}" | jq -r '.findings[1].category')" = "other" ]
    [ "$(echo "${output}" | jq -r '.findings[1].line')" = "null" ]
}

@test "fails closed when the transcript has no parseable JSON" {
    printf 'Some prose with no JSON block at all.\n' > "${BATS_TMPDIR}/lens-garbage.txt"
    LENS_RAW_OUTPUT="${BATS_TMPDIR}/lens-garbage.txt" \
        run bash "${WRAPPER}" --pr-number 1
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"no lens JSON"* ]]
}

@test "fails closed when findings is not an array" {
    printf '```json\n{"summary": "x", "findings": "nope"}\n```\n' > "${BATS_TMPDIR}/lens-badshape.txt"
    LENS_RAW_OUTPUT="${BATS_TMPDIR}/lens-badshape.txt" \
        run bash "${WRAPPER}" --pr-number 1
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"array"* ]]
}

@test "emitted JSON validates against the lens response schema" {
    # End-to-end contract check against the controller's schema, mirroring the
    # companion pytest for the adversarial wrapper.
    command -v python3 >/dev/null 2>&1 || skip "python3 not available"
    LENS_RAW_OUTPUT="${FIXTURES}/lens-findings.txt" \
        run bash "${WRAPPER}" --pr-number 7
    [ "${status}" -eq 0 ]
    SCHEMA="${BATS_TEST_DIRNAME}/../controller/src/sdlc/schemas/overengineering-lens-response.schema.json"
    printf '%s' "${output}" > "${BATS_TMPDIR}/lens-out.json"
    run python3 - "${SCHEMA}" "${BATS_TMPDIR}/lens-out.json" <<'PY'
import json, sys
schema = json.load(open(sys.argv[1]))
data = json.load(open(sys.argv[2]))
assert set(schema["required"]).issubset(data), "missing required keys"
assert isinstance(data["findings"], list)
enum = schema["properties"]["findings"]["items"]["properties"]["category"]["enum"]
for f in data["findings"]:
    assert f["category"] in enum, f["category"]
    assert f["file"] and f["reason"]
print("schema-ok")
PY
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"schema-ok"* ]]
}
