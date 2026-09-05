#!/usr/bin/env bats
# Tests for scripts/opencode-build-adapter.sh (Story 29.2-001).
#
# The wrapper reads the controller prompt on stdin, invokes OpenCode headlessly
# as `opencode run --pure ... -- "$prompt"`, and forwards stdout — stripped of
# ANSI colour, which OpenCode 1.18.15 emits even off a TTY — so the
# harness-neutral <<<RESULT_JSON>>> block reaches the controller's codex-exec
# parser unchanged.

WRAPPER="${BATS_TEST_DIRNAME}/../scripts/opencode-build-adapter.sh"

setup() {
    TEST_BIN="${BATS_TEST_TMPDIR}/bin"
    mkdir -p "${TEST_BIN}"
    export PATH="${TEST_BIN}:${PATH}"
    export OPENCODE_ARG_LOG="${BATS_TEST_TMPDIR}/opencode-args.log"

    cat > "${TEST_BIN}/opencode" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "${OPENCODE_ARG_LOG}"
cat <<'RESULT'
opencode reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"feature/opencode","build_status":"SUCCESS","commit_sha":"feedface"}
<<<END_RESULT>>>
RESULT
EOF
    chmod +x "${TEST_BIN}/opencode"
}

@test "--self-test emits a schema-valid result block" {
    run bash "${WRAPPER}" --self-test
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    [[ "${output}" == *"<<<END_RESULT>>>"* ]]
    [[ "${output}" == *'"build_status": "SUCCESS"'* ]]
}

@test "--self-test runs without a configured opencode binary" {
    run bash -c "PATH=/usr/bin:/bin bash '${WRAPPER}' --self-test"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
}

@test "passes the stdin prompt as a positional message argument via --pure" {
    local prompt="build story 29.2-001 with some context"

    run bash -c "printf '%s' \"\$1\" | bash '${WRAPPER}'" _ "${prompt}"

    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    run cat "${OPENCODE_ARG_LOG}"
    [[ "${output}" == *"run"* ]]
    [[ "${output}" == *"--pure"* ]]
    [ "$(tail -n1 "${OPENCODE_ARG_LOG}")" = "${prompt}" ]
}

@test "preserves a multiline prompt intact as the trailing argument" {
    local prompt=$'build story 29.2-001\nwith multiline context'

    run bash -c "printf '%s' \"\$1\" | bash '${WRAPPER}'" _ "${prompt}"

    [ "${status}" -eq 0 ]
    # The prompt is the wrapper's own final positional arg (after `--`), which
    # the fake CLI logs on its own trailing line(s) — the log's tail must equal
    # the prompt verbatim, newline included, not truncated to one line.
    logged="$(tail -n2 "${OPENCODE_ARG_LOG}")"
    [ "${logged}" = "${prompt}" ]
}

@test "honors OPENCODE_BIN and OPENCODE_FLAGS before the prompt" {
    mv "${TEST_BIN}/opencode" "${TEST_BIN}/fake-opencode"

    run bash -c "printf 'prompt' | OPENCODE_BIN=fake-opencode OPENCODE_FLAGS='--dir /work' bash '${WRAPPER}'"

    [ "${status}" -eq 0 ]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"--dir"* ]]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"/work"* ]]
    [ "$(tail -n1 "${OPENCODE_ARG_LOG}")" = "prompt" ]
}

@test "forwards --model to the underlying opencode invocation" {
    run bash -c "echo prompt | bash '${WRAPPER}' --model openai/gpt-5.6"

    [ "${status}" -eq 0 ]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"--model"* ]]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"openai/gpt-5.6"* ]]
}

@test "accepts --model=<id> form" {
    run bash -c "echo prompt | bash '${WRAPPER}' --model=openai/gpt-5.6"

    [ "${status}" -eq 0 ]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"openai/gpt-5.6"* ]]
}

@test "rejects --model with no value" {
    run bash "${WRAPPER}" --model
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"--model needs a value"* ]]
}

@test "a failing opencode command is a non-zero dispatch failure" {
    cat > "${TEST_BIN}/opencode" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
    chmod +x "${TEST_BIN}/opencode"

    run bash -c "echo prompt | bash '${WRAPPER}'"

    [ "${status}" -ne 0 ]
}

@test "rejects an unexpected argument (the prompt is read from stdin)" {
    run bash "${WRAPPER}" --bogus
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"unexpected argument"* ]]
}

@test "strips ANSI escapes so the result block still parses" {
    esc=$'\033'
    cat > "${TEST_BIN}/opencode" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "\${OPENCODE_ARG_LOG}"
printf '${esc}[2mthinking...${esc}[0m\n'
printf '${esc}[1m<<<RESULT_JSON>>>${esc}[0m\n'
printf '{"branch_name":"feature/opencode","build_status":"SUCCESS","commit_sha":"feedface"}\n'
printf '${esc}[1m<<<END_RESULT>>>${esc}[0m\n'
EOF
    chmod +x "${TEST_BIN}/opencode"

    run bash -c "echo prompt | bash '${WRAPPER}'"

    [ "${status}" -eq 0 ]
    [[ "${output}" != *$'\033'* ]]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    [[ "${output}" == *'"build_status":"SUCCESS"'* ]]
}
