#!/usr/bin/env bats
# Tests for the Telegram JSON-escaping path of cmux-bridge.sh (Story 1.3-001).
#
# Strategy: stub `curl` on PATH so it dumps the JSON payload passed via `-d`
# to a file instead of hitting the network. The payload is then validated
# with `jq` and inspected for the expected escaped content.

BRIDGE="${BATS_TEST_DIRNAME}/../hooks/cmux-bridge.sh"
FIXTURE="${BATS_TEST_DIRNAME}/fixtures/adversarial-strings.txt"

setup() {
    STUB_DIR="$(mktemp -d)"
    PAYLOAD_FILE="${STUB_DIR}/payload.json"

    # Stub curl: capture the value following `-d` into PAYLOAD_FILE.
    cat > "${STUB_DIR}/curl" <<EOF
#!/bin/bash
prev=""
for arg in "\$@"; do
    if [ "\$prev" = "-d" ]; then
        printf '%s' "\$arg" > "${PAYLOAD_FILE}"
    fi
    prev="\$arg"
done
exit 0
EOF
    chmod +x "${STUB_DIR}/curl"

    export PATH="${STUB_DIR}:${PATH}"
    export TELEGRAM_BOT_TOKEN="stub-token"
    export TELEGRAM_CHAT_ID="stub-chat"
    # Force the graceful-degradation Telegram path (cmux absent).
    unset CMUX_SOCKET_PATH
}

teardown() {
    rm -rf "${STUB_DIR}"
}

@test "emits valid JSON for a plain message" {
    run bash "${BRIDGE}" telegram "Hello" "World"
    [ "$status" -eq 0 ]
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
}

@test "emits valid JSON when body contains adversarial characters" {
    body="$(cat "${FIXTURE}")"
    run bash "${BRIDGE}" telegram "Adversarial * _title_ \"quote\"" "${body}"
    [ "$status" -eq 0 ]
    # The captured payload must be parseable JSON despite quotes/backslashes/newlines.
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
}

@test "preserves adversarial content faithfully through JSON round-trip" {
    body="$(cat "${FIXTURE}")"
    bash "${BRIDGE}" telegram "T" "${body}"
    decoded="$(jq -r .text "${PAYLOAD_FILE}")"
    # Every fixture line must survive intact inside the decoded text field.
    [[ "$decoded" == *'double "quotes" inside'* ]]
    [[ "$decoded" == *'back\slash and \n literal'* ]]
    [[ "$decoded" == *'backticks `code` and *asterisks*'* ]]
    [[ "$decoded" == *'🚨'* ]]
}

@test "drops parse_mode for MVP plain-text delivery" {
    bash "${BRIDGE}" telegram "T" "B"
    run jq -e 'has("parse_mode")' "${PAYLOAD_FILE}"
    # parse_mode must be absent.
    [ "$status" -ne 0 ]
}

@test "JSON contains exactly chat_id and text keys" {
    bash "${BRIDGE}" telegram "T" "B"
    run jq -r 'keys | sort | join(",")' "${PAYLOAD_FILE}"
    [ "$output" = "chat_id,text" ]
}

# ---------------------------------------------------------------------------
# Gap tests added by QA coverage gate (Story 1.3-001)
# ---------------------------------------------------------------------------

@test "emits valid JSON when body contains control characters (NUL, BEL, ESC)" {
    # Control chars that previously broke string interpolation must be escaped
    # by jq into \u00XX sequences, keeping the payload valid JSON.
    body="$(printf 'before\x01NUL-like\x07BEL\x1b[31mESC-seq\x1b[0mafter')"
    bash "${BRIDGE}" telegram "ctrl-title" "${body}"
    # Payload must be parseable despite embedded control characters.
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
    # The text field must be present and non-empty.
    run jq -r '.text' "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"before"* ]]
    [[ "$output" == *"after"* ]]
}

@test "emits valid JSON when both title and body are empty strings" {
    # Empty args must still produce well-formed JSON — no crash or malformed output.
    bash "${BRIDGE}" telegram "" ""
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
    run jq -r 'keys | sort | join(",")' "${PAYLOAD_FILE}"
    [ "$output" = "chat_id,text" ]
}

@test "emits valid JSON for a very long body (~10 KB)" {
    # jq --arg must handle large payloads without truncation or arg-length errors.
    long_body="$(python3 -c "print('x' * 10240)")"
    bash "${BRIDGE}" telegram "long-test" "${long_body}"
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
    # Confirm no truncation: decoded text length should exceed 10240 chars.
    decoded_len="$(jq -r '.text' "${PAYLOAD_FILE}" | wc -c | tr -d ' ')"
    [ "${decoded_len}" -gt 10240 ]
}

@test "does not call curl when TELEGRAM_BOT_TOKEN is unset" {
    # When credentials are absent the bridge must skip the send entirely.
    # We detect this by verifying the payload file is never created.
    # Override HOME so the `source ~/.claude/config/.env` in _send_telegram
    # finds no .env file and cannot re-inject the token from disk.
    local FAKE_HOME="${STUB_DIR}/fakehome"
    mkdir -p "${FAKE_HOME}"
    unset TELEGRAM_BOT_TOKEN
    unset TELEGRAM_CHAT_ID
    rm -f "${PAYLOAD_FILE}"
    HOME="${FAKE_HOME}" bash "${BRIDGE}" telegram "no-creds" "body"
    # curl stub was never invoked — payload file must not exist.
    [ ! -f "${PAYLOAD_FILE}" ]
}

@test "emits valid JSON via cmux-present telegram subcommand path" {
    # When cmux IS on PATH and CMUX_SOCKET_PATH is set the script reaches the
    # case block at the bottom instead of the graceful-degradation exit.
    # Both code paths call _send_telegram — this test exercises the cmux branch.
    local CMUX_STUB="${STUB_DIR}/cmux"
    printf '#!/bin/bash\nexit 0\n' > "${CMUX_STUB}"
    chmod +x "${CMUX_STUB}"
    CMUX_SOCKET_PATH="/tmp/fake-cmux.sock" \
        bash "${BRIDGE}" telegram "cmux-path-title" "cmux-path-body"
    run jq -e . "${PAYLOAD_FILE}"
    [ "$status" -eq 0 ]
    run jq -r '.text' "${PAYLOAD_FILE}"
    [[ "$output" == *"cmux-path-title"* ]]
    [[ "$output" == *"cmux-path-body"* ]]
}
