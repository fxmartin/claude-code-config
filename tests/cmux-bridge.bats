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
