#!/usr/bin/env bats
# ABOUTME: Tests for notify-telegram.sh — the standalone, cmux-free Telegram sender.
# ABOUTME: Exercises credential resolution, no-op behavior, and JSON-safe escaping.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    NOTIFY="$REPO_ROOT/hooks/notify-telegram.sh"
    # Isolated fake HOME so we never touch the real ~/.claude.
    FAKE_HOME="$(mktemp -d)"
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "$FAKE_HOME"
}

# Lay down ~/.claude/config/.env with the given token under the fake HOME.
_stage_env_file() {
    local token="$1"
    mkdir -p "$FAKE_HOME/.claude/config"
    cat > "$FAKE_HOME/.claude/config/.env" <<EOF
TELEGRAM_BOT_TOKEN="$token"
TELEGRAM_CHAT_ID="999"
EOF
}

@test "dry-run resolves credentials from the environment" {
    run env HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        TELEGRAM_BOT_TOKEN="env-token-123" TELEGRAM_CHAT_ID="42" \
        bash "$NOTIFY" "Test" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"env-token-123"* ]]
    [[ "$output" == *"chat=42"* ]]
}

@test "dry-run resolves credentials from ~/.claude/config/.env" {
    _stage_env_file "file-token-456"
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
        HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        bash "$NOTIFY" "Test" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"file-token-456"* ]]
}

@test "environment credentials win over the .env file" {
    _stage_env_file "stale-file-token"
    run env HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        TELEGRAM_BOT_TOKEN="env-wins-token" TELEGRAM_CHAT_ID="7" \
        bash "$NOTIFY" "Test" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"env-wins-token"* ]]
    [[ "$output" != *"stale-file-token"* ]]
}

@test "silent no-op when no .env and no token exist" {
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
        HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        bash "$NOTIFY" "Test" "Body"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "no-op when only the token is set (chat id missing)" {
    run env -u TELEGRAM_CHAT_ID \
        HOME="$FAKE_HOME" NOTIFY_DRYRUN=1 \
        TELEGRAM_BOT_TOKEN="lonely-token" \
        bash "$NOTIFY" "Test" "Body"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# Stub curl on PATH so the configured (non-dry-run) path builds the jq payload
# and "sends" without touching the network. The stub records the payload it was
# handed so we can assert the JSON is well-formed and correctly escaped.
_stub_curl() {
    STUB_DIR="$FAKE_HOME/bin"
    mkdir -p "$STUB_DIR"
    cat > "$STUB_DIR/curl" <<EOF
#!/usr/bin/env bash
# Capture the -d payload argument into a fixture file, then succeed.
prev=""
for arg in "\$@"; do
    if [ "\$prev" = "-d" ]; then
        printf '%s' "\$arg" > "$FAKE_HOME/payload.json"
    fi
    prev="\$arg"
done
exit 0
EOF
    chmod +x "$STUB_DIR/curl"
}

@test "builds JSON-safe payload for a title and body containing quotes" {
    _stub_curl
    run env HOME="$FAKE_HOME" PATH="$STUB_DIR:$PATH" \
        TELEGRAM_BOT_TOKEN="000:bogus" TELEGRAM_CHAT_ID="1" \
        bash "$NOTIFY" 'Title with "quotes" & $special' 'Body with "quotes"
and a newline'
    [ "$status" -eq 0 ]
    # The captured payload must be valid JSON (jq escaped the metacharacters).
    run jq -e . "$FAKE_HOME/payload.json"
    [ "$status" -eq 0 ]
    # And the chat id and quoted text survived intact through the escaping.
    run jq -r '.chat_id' "$FAKE_HOME/payload.json"
    [ "$output" = "1" ]
    run jq -r '.text' "$FAKE_HOME/payload.json"
    [[ "$output" == *'"quotes"'* ]]
}

@test "exits 0 even when unconfigured and given odd input" {
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
        HOME="$FAKE_HOME" \
        bash "$NOTIFY" 'A "weird" title' 'multi
line $body'
    [ "$status" -eq 0 ]
}
