#!/usr/bin/env bats
# Tests for scripts/qwen-build-adapter.sh.
#
# The wrapper reads the controller prompt on stdin, invokes Qwen Code headlessly
# as `qwen -p "$prompt"`, and forwards stdout so the harness-neutral
# <<<RESULT_JSON>>> block reaches the controller parser unchanged.

WRAPPER="${BATS_TEST_DIRNAME}/../scripts/qwen-build-adapter.sh"

setup() {
    TEST_BIN="${BATS_TEST_TMPDIR}/bin"
    mkdir -p "${TEST_BIN}"
    export PATH="${TEST_BIN}:${PATH}"
    export QWEN_ARG_LOG="${BATS_TEST_TMPDIR}/qwen-args.log"

    cat > "${TEST_BIN}/qwen" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "${QWEN_ARG_LOG}"
cat <<'RESULT'
qwen reasoning prose
<<<RESULT_JSON>>>
{"branch_name":"feature/qwen","build_status":"SUCCESS","commit_sha":"feedface"}
<<<END_RESULT>>>
RESULT
EOF
    chmod +x "${TEST_BIN}/qwen"
}

@test "--self-test emits a schema-valid result block" {
    run bash "${WRAPPER}" --self-test
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    [[ "${output}" == *"<<<END_RESULT>>>"* ]]
    [[ "${output}" == *'"build_status": "SUCCESS"'* ]]
}

@test "passes stdin prompt to qwen headless mode" {
    local prompt=$'build story 20.7-001\nwith multiline context'

    run bash -c "printf '%s' \"\$1\" | bash '${WRAPPER}'" _ "${prompt}"

    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    [ "$(sed -n '1p' "${QWEN_ARG_LOG}")" = "-p" ]
    [ "$(sed -n '2,$p' "${QWEN_ARG_LOG}")" = "${prompt}" ]
}

@test "honors QWEN_BIN and QWEN_FLAGS before the prompt" {
    mv "${TEST_BIN}/qwen" "${TEST_BIN}/fake-qwen"

    run bash -c "printf 'prompt' | QWEN_BIN=fake-qwen QWEN_FLAGS='--model qwen3-coder' bash '${WRAPPER}'"

    [ "${status}" -eq 0 ]
    [ "$(sed -n '1p' "${QWEN_ARG_LOG}")" = "--model" ]
    [ "$(sed -n '2p' "${QWEN_ARG_LOG}")" = "qwen3-coder" ]
    [ "$(sed -n '3p' "${QWEN_ARG_LOG}")" = "-p" ]
    [ "$(sed -n '4p' "${QWEN_ARG_LOG}")" = "prompt" ]
}

@test "a failing qwen command is a non-zero dispatch failure" {
    cat > "${TEST_BIN}/qwen" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
    chmod +x "${TEST_BIN}/qwen"

    run bash -c "echo prompt | bash '${WRAPPER}'"

    [ "${status}" -ne 0 ]
}

@test "rejects an unexpected argument (the prompt is read from stdin)" {
    run bash "${WRAPPER}" --bogus
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"unexpected argument"* ]]
}
