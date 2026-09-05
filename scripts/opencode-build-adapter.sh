#!/usr/bin/env bash
#
# ABOUTME: OpenCode build/QA adapter — runs a controller agent through
# ABOUTME: `opencode run` and forwards its <<<RESULT_JSON>>> contract to the codex-exec parser.
#
# The controller writes the assembled role prompt to this wrapper's stdin, and
# the wrapper lets it flow straight through: `opencode run` reads its message
# from stdin when given no positional (verified against OpenCode 1.18.15 —
# `printf '...' | opencode run --pure --model anthropic/claude-haiku-4-5`
# answers from the model). An earlier revision passed the prompt as a trailing
# `--` positional on the belief that stdin "never reaches the model"; that
# reading came from an *empty* stdin, which fails with "You must provide a
# message or a command" for a different reason. Positional delivery also caps
# the prompt at Linux's MAX_ARG_STRLEN (128 KiB for a single argument, whatever
# ARG_MAX allows), so a large re-ask or bugfix prompt would die with E2BIG;
# stdin has no such ceiling. `--pure` disables external plugins so plugin
# chatter cannot land in
# stdout. OpenCode's stdout is forwarded with ANSI colour stripped (OpenCode
# emits CSI escapes even when stdout is not a TTY; forwarded verbatim they land
# inside the result block and make the controller's `parse_and_validate` reject
# valid JSON), so the final <<<RESULT_JSON>>> ... <<<END_RESULT>>> block
# round-trips to the controller's `codex-exec` parser.
#
# REQUIRED for unattended dispatch: this repo's `opencode.json` must allow the
# permissions OpenCode enforces even in headless `run` mode, e.g.:
#   {
#     "permission": { "edit": "allow", "bash": "allow" }
#   }
# Without it, OpenCode blocks the run on an interactive approval prompt it has
# no TTY to answer — verified: a run with no permission config hangs
# indefinitely with zero output rather than failing fast. `--auto` (auto-approve
# anything not explicitly denied) is a coarser alternative that avoids the
# config file, but prefer the explicit permission block above outside a
# disposable sandbox.
#
# Usage:
#   echo "<agent prompt>" | opencode-build-adapter.sh [--model provider/model]
#
# Arguments:
#   --model <id>   Per-stage model routing (mirrors the codex adapter): the
#                  controller substitutes the stage's mapped model into the
#                  `{model}` placeholder of the registry command and passes it
#                  here as `provider/model` (e.g. `openai/gpt-5.6`); the wrapper
#                  forwards it as `opencode run --model <id>`. Omitted when the
#                  harness routes no per-stage model.
#
# Environment:
#   OPENCODE_BIN     Override the OpenCode executable path/name (default
#                    `opencode`).
#   OPENCODE_FLAGS   Extra flags inserted before the prompt, word-split
#                    intentionally for simple flag strings, e.g. '--dir /work'.
#
# Exit status:
#   0  forwarded OpenCode's output (the controller's parser validates the block)
#   2  usage error (an unexpected argument, or --model with no value)
#   *  whatever the underlying `opencode run` exits with (a non-zero exit is a
#      dispatch failure the controller surfaces)

set -euo pipefail

OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
OPENCODE_FLAGS_STRING="${OPENCODE_FLAGS:-}"

# --self-test: emit a minimal, schema-valid build result block and exit, proving
# the contract round-trips without invoking any real OpenCode CLI.
if [ "${1:-}" = "--self-test" ]; then
  cat <<'EOF'
OpenCode would print its human-readable reasoning here; the controller ignores
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

MODEL=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      shift
      MODEL="${1:-}"
      if [ -z "$MODEL" ]; then
        echo "opencode-build-adapter: --model needs a value" >&2
        exit 2
      fi
      ;;
    --model=*)
      MODEL="${1#--model=}"
      if [ -z "$MODEL" ]; then
        echo "opencode-build-adapter: --model needs a value" >&2
        exit 2
      fi
      ;;
    *)
      echo "opencode-build-adapter: unexpected argument: $1 (the prompt is read from stdin)" >&2
      exit 2
      ;;
  esac
  shift
done

# The prompt is NOT slurped here: stdin is inherited across the `exec` below and
# read by OpenCode itself, so an arbitrarily large prompt never has to fit in an
# argv slot.

# shellcheck disable=SC2206
opencode_flags=(${OPENCODE_FLAGS_STRING})

model_flags=()
if [ -n "$MODEL" ]; then
  model_flags=(--model "$MODEL")
fi

# Redirect stdout through the ANSI filter FIRST, then `exec` the CLI, so the
# wrapper hands over its own PID instead of staying alive as a pipeline parent
# (matching the codex and qwen adapters). The controller dispatches this wrapper
# on the captured path, where a timeout reaps only the direct child — a pipeline
# would leave OpenCode orphaned and still writing to the worktree while the
# controller retries. `sed` exits on EOF once the exec'd CLI is gone.
esc=$(printf '\033')
exec > >(sed -e "s/${esc}\[[0-9;?]*[a-zA-Z]//g")
exec "${OPENCODE_BIN}" run --pure "${opencode_flags[@]}" "${model_flags[@]}"
