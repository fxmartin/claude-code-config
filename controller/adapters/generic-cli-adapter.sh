#!/usr/bin/env bash
# ABOUTME: Generic CLI harness adapter template — wraps any headless agent CLI so
# ABOUTME: it speaks the controller's stdin-prompt / <<<RESULT_JSON>>> contract (Story 20.6-001).
#
# Copy this file to controller/adapters/<harness>-adapter.sh, set the one
# AGENT_CMD line below (or export HARNESS_AGENT_CMD), and point a harnesses.yaml
# `command:` at the copy. No controller (Python) changes are required — see
# docs/harness-adapters.md for the full walkthrough.
#
# The controller contract this template implements:
#   1. The prompt is delivered on the wrapper's STDIN.
#   2. The wrapper runs the underlying agent CLI headlessly (no TTY, no prompts).
#   3. The agent's final answer must contain a result block:
#        <<<RESULT_JSON>>>
#        { ...the agent-response JSON for the dispatched role... }
#        <<<END_RESULT>>>
#      The wrapper forwards the CLI's stdout verbatim, so a block the CLI emits
#      round-trips untouched to the controller's parser (parser: codex-exec).
#   4. A non-zero exit is a dispatch failure; exit 0 only when the run produced
#      a result block.
#
# `--self-test` proves the round-trip out of the box: it emits a schema-valid
# build-agent result block without invoking any real CLI.
set -euo pipefail

# The underlying agent CLI. It must accept the prompt on its stdin and run
# headless. Edit this line per harness, or override at dispatch time by exporting
# HARNESS_AGENT_CMD. Examples:
#   AGENT_CMD="codex exec"
#   AGENT_CMD="opencode run --quiet"
AGENT_CMD="${HARNESS_AGENT_CMD:-}"

# ---------------------------------------------------------------------------
# --self-test: emit a minimal, schema-valid result block and exit. This is the
# "round-trips the contract out of the box" proof — copy the template, run
# `./generic-cli-adapter.sh --self-test`, and the controller's contract parser
# accepts the output unedited.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--self-test" ]; then
  cat <<'EOF'
The agent would print its human-readable reasoning here; the controller ignores
everything outside the result block below.

<<<RESULT_JSON>>>
{
  "branch_name": "feature/example-0.0-000",
  "build_status": "SUCCESS",
  "commit_sha": "0000000000000000000000000000000000000000"
}
<<<END_RESULT>>>
EOF
  exit 0
fi

# Fail fast (no silent run) when the template was wired into a harness but the
# underlying CLI was never set. The message names the exact knob to fix.
if [ -z "$AGENT_CMD" ]; then
  echo "generic-cli-adapter: no agent CLI configured." >&2
  echo "Set AGENT_CMD in this script or export HARNESS_AGENT_CMD before dispatch." >&2
  exit 64
fi

# The controller delivers the prompt on stdin; hand it to the agent CLI on its
# stdin and forward the CLI's stdout verbatim so the result block round-trips.
exec bash -c "$AGENT_CMD"
