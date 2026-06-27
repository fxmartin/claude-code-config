#!/usr/bin/env bats
# Tests for scripts/codex-build-adapter.sh (Story 20.3-001).
#
# The wrapper is the Codex build/QA adapter for the harness registry (Story
# 20.1-001). It reads the agent prompt on stdin, runs Codex via `codex exec`,
# and forwards Codex's stdout — carrying the harness-neutral
# <<<RESULT_JSON>>> ... <<<END_RESULT>>> block — verbatim to the controller's
# `codex-exec` output parser (Story 20.1-002).
#
# Strategy: hermetic only. `--self-test` proves the contract round-trips with no
# real Codex, and HARNESS_AGENT_CMD (the wrapper's documented override) lets us
# substitute a trivial command for `codex exec` so the stdin->CLI->stdout forward
# is exercised without invoking Codex. A companion controller pytest
# (controller/tests/test_codex_adapter.py) asserts the schema round-trip and the
# zero-claude property end to end.

WRAPPER="${BATS_TEST_DIRNAME}/../scripts/codex-build-adapter.sh"

@test "--self-test emits a schema-valid result block" {
    run bash "${WRAPPER}" --self-test
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    [[ "${output}" == *"<<<END_RESULT>>>"* ]]
    [[ "${output}" == *'"build_status": "SUCCESS"'* ]]
}

@test "forwards the agent's result block verbatim from stdin" {
    transcript=$'codex reasoning prose\n<<<RESULT_JSON>>>\n{"branch_name":"feature/20.3-001","build_status":"SUCCESS","commit_sha":"deadbeef"}\n<<<END_RESULT>>>'
    # HARNESS_AGENT_CMD=cat stands in for `codex exec`: it reads the prompt on
    # stdin and echoes it, so the wrapper's stdin->CLI->stdout forward is exercised.
    run bash -c "printf '%s' \"\$1\" | HARNESS_AGENT_CMD=cat bash '${WRAPPER}'" _ "${transcript}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    block="$(printf '%s\n' "${output}" | sed -n '/<<<RESULT_JSON>>>/,/<<<END_RESULT>>>/p' | sed '1d;$d')"
    echo "${block}" | jq -e 'type == "object"' > /dev/null
    [ "$(echo "${block}" | jq -r .commit_sha)" = "deadbeef" ]
}

@test "a failing underlying command is a non-zero dispatch failure" {
    run bash -c "echo prompt | HARNESS_AGENT_CMD='exit 3' bash '${WRAPPER}'"
    [ "${status}" -ne 0 ]
}

@test "rejects an unexpected argument (the prompt is read from stdin)" {
    run bash "${WRAPPER}" --bogus
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"unexpected argument"* ]]
}

# Story 20.7-004: per-stage model routing — the controller substitutes the
# stage's mapped model into the `{model}` placeholder and passes `--model <id>`,
# which the wrapper must forward to the underlying command.
@test "forwards --model to the underlying command" {
    # HARNESS_AGENT_CMD=echo stands in for `codex exec`: it prints its argv, so we
    # can assert the model id reached the underlying command.
    run bash -c "echo prompt | HARNESS_AGENT_CMD=echo bash '${WRAPPER}' --model gpt-5.4-codex"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"--model gpt-5.4-codex"* ]]
}

@test "accepts --model=<id> form" {
    run bash -c "echo prompt | HARNESS_AGENT_CMD=echo bash '${WRAPPER}' --model=gpt-5.4-codex-mini"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"--model gpt-5.4-codex-mini"* ]]
}

@test "rejects --model with no value" {
    run bash -c "echo prompt | bash '${WRAPPER}' --model"
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"--model needs a value"* ]]
}

@test "no --model still runs the bare command (no regression)" {
    transcript=$'p\n<<<RESULT_JSON>>>\n{"branch_name":"feature/x","build_status":"SUCCESS","commit_sha":"abc"}\n<<<END_RESULT>>>'
    run bash -c "printf '%s' \"\$1\" | HARNESS_AGENT_CMD=cat bash '${WRAPPER}'" _ "${transcript}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
}
