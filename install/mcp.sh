#!/usr/bin/env bash
# ABOUTME: --mcp mode — merge mcp/config.template.json into ~/.claude.json.
# ABOUTME: Idempotent: merging the same template twice yields identical JSON.
#
# Sourced by install.sh after common.sh. Expects SCRIPT_DIR, CLAUDE_JSON, DRY_RUN.

install_mcp_run() {
  echo ""
  echo "[mcp] Configuring MCP servers..."

  # Load .env if present and the caller did not explicitly opt out (tests do).
  if [ -f "$SCRIPT_DIR/.env" ] && [ -z "${CLAUDE_CONFIG_NO_ENV:-}" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    info "Loaded .env"
  fi

  if [ -z "${BROWSER_PATH:-}" ]; then
    warn "BROWSER_PATH not set. Create .env from .env.example or export BROWSER_PATH"
    warn "MCP config will have empty browser path"
    BROWSER_PATH=""
  fi

  local template="$SCRIPT_DIR/mcp/config.template.json"
  if [ ! -f "$template" ]; then
    warn "MCP template not found: $template"
    return
  fi

  local mcp_config
  mcp_config=$(sed "s|\\\$BROWSER_PATH|$BROWSER_PATH|g" "$template")

  if ! command -v jq &>/dev/null; then
    warn "jq not found — cannot configure MCP servers. Install jq or skip --mcp."
    return
  fi

  if [ -f "$CLAUDE_JSON" ]; then
    # Merge new mcpServers into the existing file. jq's pretty-printer is the
    # canonical form — both the first and the Nth run must produce identical
    # JSON for the mode to be idempotent.
    local merged
    merged=$(jq -s '
      .[0] as $existing |
      .[1].mcpServers as $newServers |
      $existing * {mcpServers: (($existing.mcpServers // {}) * $newServers)}
    ' "$CLAUDE_JSON" <(echo "$mcp_config"))
    if [ "${DRY_RUN:-false}" = "true" ]; then
      echo "  [dry-run] write merged MCP config → $CLAUDE_JSON"
    else
      echo "$merged" > "$CLAUDE_JSON"
    fi
    info "Merged MCP servers into $CLAUDE_JSON"
  else
    # Even on first-run, normalise through jq so a subsequent merge produces
    # byte-identical output (the legacy script wrote sed's compact form and
    # then jq's pretty form, which broke idempotency).
    local normalized
    normalized=$(echo "$mcp_config" | jq '.')
    if [ "${DRY_RUN:-false}" = "true" ]; then
      echo "  [dry-run] write new MCP config → $CLAUDE_JSON"
    else
      echo "$normalized" > "$CLAUDE_JSON"
    fi
    info "Created $CLAUDE_JSON with MCP servers"
  fi
}
