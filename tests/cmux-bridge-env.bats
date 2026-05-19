#!/usr/bin/env bats
# Story 1.3-002 — .env source path bug.
# cmux-bridge.sh must source the .env that install.sh actually writes
# ($SCRIPT_DIR/.env, i.e. relative to the script's own directory).
# Before the fix the bridge sourced ~/.claude/config/.env, which install.sh
# never creates, so TELEGRAM_BOT_TOKEN set only in .env was silently dropped.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    BRIDGE="$REPO_ROOT/hooks/cmux-bridge.sh"
    # Isolated fake HOME so we never touch the real ~/.claude.
    FAKE_HOME="$(mktemp -d)"
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "$FAKE_HOME"
}

# Simulate the installed layout: hooks/ and .env as siblings, exactly as
# install.sh lays them out under $SCRIPT_DIR.
_stage_install() {
    local token="$1"
    INSTALL_DIR="$FAKE_HOME/repo"
    mkdir -p "$INSTALL_DIR/hooks"
    cp "$BRIDGE" "$INSTALL_DIR/hooks/cmux-bridge.sh"
    chmod +x "$INSTALL_DIR/hooks/cmux-bridge.sh"
    cat > "$INSTALL_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN="$token"
TELEGRAM_CHAT_ID="999"
EOF
}

@test "bridge resolves .env relative to its own script directory" {
    _stage_install "secret-token-123"
    # No TELEGRAM_* in the environment, no cmux socket: graceful-degradation
    # path runs _send_telegram, which must load the sibling .env.
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID -u CMUX_SOCKET_PATH \
        HOME="$FAKE_HOME" CMUX_BRIDGE_DRYRUN=1 \
        bash "$INSTALL_DIR/hooks/cmux-bridge.sh" telegram "Test" "Body"
    [ "$status" -eq 0 ]
    # In dry-run mode the bridge prints the token it would use.
    [[ "$output" == *"secret-token-123"* ]]
}

@test "bridge does not depend on ~/.claude/config/.env" {
    _stage_install "from-sibling-env"
    # Plant a decoy at the OLD (buggy) path with a different token.
    mkdir -p "$FAKE_HOME/.claude/config"
    echo 'TELEGRAM_BOT_TOKEN="stale-wrong-token"' > "$FAKE_HOME/.claude/config/.env"
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID -u CMUX_SOCKET_PATH \
        HOME="$FAKE_HOME" CMUX_BRIDGE_DRYRUN=1 \
        bash "$INSTALL_DIR/hooks/cmux-bridge.sh" telegram "Test" "Body"
    [ "$status" -eq 0 ]
    [[ "$output" == *"from-sibling-env"* ]]
    [[ "$output" != *"stale-wrong-token"* ]]
}

@test "bridge stays silent when no .env and no token exist" {
    INSTALL_DIR="$FAKE_HOME/repo"
    mkdir -p "$INSTALL_DIR/hooks"
    cp "$BRIDGE" "$INSTALL_DIR/hooks/cmux-bridge.sh"
    run env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID -u CMUX_SOCKET_PATH \
        HOME="$FAKE_HOME" CMUX_BRIDGE_DRYRUN=1 \
        bash "$INSTALL_DIR/hooks/cmux-bridge.sh" telegram "Test" "Body"
    [ "$status" -eq 0 ]
}
