#!/usr/bin/env bats
# Tests for scripts/validate-agent-registry.sh (Story 2.1-003).
#
# Strategy: run the validator against fixture repo roots under
# tests/fixtures/agent-registry/ — a "good" root whose references all
# resolve, and a "bad" root containing a reference to a nonexistent agent.

VALIDATOR="${BATS_TEST_DIRNAME}/../scripts/validate-agent-registry.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/agent-registry"

@test "exits 0 when every subagent_type reference resolves" {
    run "${VALIDATOR}" "${FIXTURES}/good"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

@test "exits non-zero when a subagent_type references a nonexistent agent" {
    run "${VALIDATOR}" "${FIXTURES}/bad"
    [ "${status}" -ne 0 ]
}

@test "error message names the unresolved reference with file and line" {
    run "${VALIDATOR}" "${FIXTURES}/bad"
    [[ "${output}" == *"nonexistent-agent"* ]]
    [[ "${output}" == *"skills/bad-skill.md:"* ]]
}

@test "bracketed placeholders are skipped, not flagged as unresolved" {
    # good/skills/good-skill.md contains subagent_type=[story.agent_type]
    run "${VALIDATOR}" "${FIXTURES}/good"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"story.agent_type"* ]]
}

@test "built-in subagent types resolve without an agents/ file" {
    # good/skills/good-skill.md references subagent_type="general-purpose"
    run "${VALIDATOR}" "${FIXTURES}/good"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"general-purpose"* ]]
}

@test "the real repository passes validation (zero unresolved references)" {
    run "${VALIDATOR}" "${BATS_TEST_DIRNAME}/.."
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}
