#!/usr/bin/env bash
#
# ABOUTME: Codex build/QA adapter — runs a controller agent on Codex via `codex
# ABOUTME: exec` and forwards its <<<RESULT_JSON>>> contract to the codex-exec parser (Story 20.3-001).
#
# The first concrete non-Claude build/coverage adapter for the harness registry
# (Story 20.1-001). The controller registers it in
# controller/config/harnesses.yaml as the `codex` harness's command and
# dispatches an agent by writing the assembled prompt to this wrapper's STDIN
# (the controller's dispatch contract). The wrapper implements that contract:
#
#   1. The prompt arrives on this wrapper's STDIN.
#   2. Codex runs headlessly via `codex exec`, reading that prompt on its stdin.
#   3. Codex's stdout is forwarded verbatim, so the agent's
#        <<<RESULT_JSON>>>
#        { ...the dispatched role's response JSON... }
#        <<<END_RESULT>>>
#      block round-trips untouched to the controller's `codex-exec` output parser
#      (Story 20.1-002). A non-zero exit is a dispatch failure.
#
# The prompt itself instructs the agent to end with that result block, so the
# contract is harness-neutral — the wrapper adds nothing Codex-specific to it.
# Because the wrapper only ever runs `codex`, a run routed to this harness spawns
# zero `claude` processes (Story 20.3-001 AC3).
#
# Usage:
#   echo "<agent prompt>" | codex-build-adapter.sh [--model <id>]
#
# Arguments:
#   --model <id>   Per-stage model routing (Story 20.7-004): the controller
#                  substitutes the stage's mapped model into the `{model}`
#                  placeholder of the registry command and passes it here; the
#                  wrapper forwards it as `codex exec --model <id>`. Omitted when
#                  the harness routes a single fixed model.
#
# Environment:
#   HARNESS_AGENT_CMD  Override the underlying command (default `codex exec`),
#                      e.g. "codex exec --sandbox workspace-write" for headless
#                      writes. A build whose worker pushes branches / opens PRs
#                      via `gh` needs network too, so a trusted repo typically
#                      uses "codex exec --dangerously-bypass-approvals-and-sandbox".
#                      (`--full-auto` is deprecated in modern Codex.) Word-split.
#
# Exit status:
#   0  forwarded the agent's output (the controller's parser validates the block)
#   2  usage error (an unexpected argument; the prompt is read from stdin)
#   *  whatever the underlying agent command exits with (a non-zero exit is a
#      dispatch failure the controller surfaces)

set -euo pipefail

# The underlying Codex command. `codex exec` is the non-interactive subcommand;
# it accepts the prompt on its stdin and runs headless. Override per environment
# via HARNESS_AGENT_CMD.
AGENT_CMD="${HARNESS_AGENT_CMD:-codex exec}"

# --self-test: emit a minimal, schema-valid build result block and exit, proving
# the contract round-trips without invoking any real Codex CLI.
if [ "${1:-}" = "--self-test" ]; then
  cat <<'EOF'
Codex would print its human-readable reasoning here; the controller ignores
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

# Story 20.7-004: accept an optional `--model <id>` (or `--model=<id>`) to route a
# per-stage model; any other argument is a usage error (the prompt is on stdin).
MODEL=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      shift
      MODEL="${1:-}"
      if [ -z "$MODEL" ]; then
        echo "codex-build-adapter: --model needs a value" >&2
        exit 2
      fi
      ;;
    --model=*)
      MODEL="${1#--model=}"
      if [ -z "$MODEL" ]; then
        echo "codex-build-adapter: --model needs a value" >&2
        exit 2
      fi
      ;;
    *)
      echo "codex-build-adapter: unexpected argument: $1 (the prompt is read from stdin)" >&2
      exit 2
      ;;
  esac
  shift
done

# The controller delivers the prompt on stdin; hand it to Codex on its stdin and
# forward Codex's stdout verbatim so the result block round-trips to the parser.
# When a model was routed, append `--model "<id>"` to the underlying command.
if [ -n "$MODEL" ]; then
  exec bash -c "$AGENT_CMD"' --model "$1"' _ "$MODEL"
fi
exec bash -c "$AGENT_CMD"
