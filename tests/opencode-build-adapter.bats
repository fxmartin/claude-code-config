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
    export OPENCODE_STDIN_LOG="${BATS_TEST_TMPDIR}/opencode-stdin.log"

    cat > "${TEST_BIN}/opencode" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "${OPENCODE_ARG_LOG}"
cat > "${OPENCODE_STDIN_LOG}"
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

@test "delivers the prompt to opencode on stdin, not in argv" {
    local prompt="build story 29.2-001 with some context"

    run bash -c "printf '%s' \"\$1\" | bash '${WRAPPER}'" _ "${prompt}"

    [ "${status}" -eq 0 ]
    [[ "${output}" == *"<<<RESULT_JSON>>>"* ]]
    run cat "${OPENCODE_ARG_LOG}"
    [[ "${output}" == *"run"* ]]
    [[ "${output}" == *"--pure"* ]]
    # `opencode run` reads its message from stdin when given no positional.
    # Delivering it in argv instead caps the prompt at MAX_ARG_STRLEN (128 KiB).
    [ "$(cat "${OPENCODE_STDIN_LOG}")" = "${prompt}" ]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" != *"${prompt}"* ]]
}

@test "preserves a multiline prompt intact on stdin" {
    local prompt=$'build story 29.2-001\nwith multiline context'

    run bash -c "printf '%s' \"\$1\" | bash '${WRAPPER}'" _ "${prompt}"

    [ "${status}" -eq 0 ]
    # Newlines must survive the hand-off verbatim, not be truncated or re-joined.
    [ "$(cat "${OPENCODE_STDIN_LOG}")" = "${prompt}" ]
}

@test "honors OPENCODE_BIN and OPENCODE_FLAGS" {
    mv "${TEST_BIN}/opencode" "${TEST_BIN}/fake-opencode"

    run bash -c "printf 'prompt' | OPENCODE_BIN=fake-opencode OPENCODE_FLAGS='--dir /work' bash '${WRAPPER}'"

    [ "${status}" -eq 0 ]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"--dir"* ]]
    [[ "$(cat "${OPENCODE_ARG_LOG}")" == *"/work"* ]]
    [ "$(cat "${OPENCODE_STDIN_LOG}")" = "prompt" ]
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

# Story 29.2-001 (review finding 1): the registry dispatches this wrapper by bare
# name with no `stream-json`, so the controller always takes `_dispatch_captured`
# — whose docstring states that on timeout `subprocess.run` reaps only the DIRECT
# child; process-group TERM->KILL escalation lives on the streaming path alone.
# A wrapper that ends in a pipeline therefore leaves the real CLI orphaned and
# still writing to the worktree while the controller advances to a retry: two
# writers, one repo. The wrapper must hand its own PID to the CLI via `exec`,
# exactly as the codex and qwen adapters do.
@test "the opencode CLI does not survive a kill of the wrapper (exec, not a pipeline)" {
    export OPENCODE_PIDFILE="${BATS_TEST_TMPDIR}/cli.pid"
    cat > "${TEST_BIN}/opencode" <<'EOF'
#!/usr/bin/env bash
echo $$ > "${OPENCODE_PIDFILE}"
exec sleep 300
EOF
    chmod +x "${TEST_BIN}/opencode"

    printf 'prompt' | bash "${WRAPPER}" >/dev/null 2>&1 &
    wrapper_pid=$!

    # Event-based wait with headroom (CI runs on modest, shared containers).
    for _ in $(seq 1 100); do
        [ -s "${OPENCODE_PIDFILE}" ] && break
        sleep 0.2
    done
    [ -s "${OPENCODE_PIDFILE}" ]
    cli_pid="$(cat "${OPENCODE_PIDFILE}")"

    # Exactly what `subprocess.run(timeout=...)` does on the captured path: kill
    # the direct child only, then reap it.
    kill -KILL "${wrapper_pid}" 2>/dev/null || true
    wait "${wrapper_pid}" 2>/dev/null || true

    for _ in $(seq 1 50); do
        kill -0 "${cli_pid}" 2>/dev/null || break
        sleep 0.2
    done

    # Clean up before asserting, so a failing run cannot leak a stray `sleep`.
    survived=0
    if kill -0 "${cli_pid}" 2>/dev/null; then
        survived=1
        kill -KILL "${cli_pid}" 2>/dev/null || true
    fi
    [ "${survived}" -eq 0 ]
}
