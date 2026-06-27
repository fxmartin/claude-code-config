#!/usr/bin/env bash
#
# ABOUTME: Qwen Code build/QA adapter — runs a controller agent through `qwen -p`.
# ABOUTME: Forwards the harness-neutral <<<RESULT_JSON>>> contract to the plain parser.
#
# The controller writes the assembled role prompt to this wrapper's stdin. Qwen
# Code's documented headless mode takes the prompt as an argument (`qwen -p
# "<prompt>"`), so the wrapper reads stdin into one prompt string and passes it
# as the final argument. Qwen's stdout is forwarded verbatim so the final
# <<<RESULT_JSON>>> ... <<<END_RESULT>>> block round-trips to the controller.
#
# Usage:
#   echo "<agent prompt>" | qwen-build-adapter.sh
#
# Environment:
#   QWEN_BIN    Override the Qwen executable path/name (default `qwen`).
#   QWEN_FLAGS  Extra flags inserted before `-p`. Word-split intentionally for
#               simple flag strings, e.g. '--model qwen3-coder'.

set -euo pipefail

QWEN_BIN="${QWEN_BIN:-qwen}"
QWEN_FLAGS_STRING="${QWEN_FLAGS:-}"

if [[ "${1:-}" = "--self-test" ]]; then
  cat <<'EOF'
Qwen Code would print its human-readable reasoning here; the controller ignores
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

if [[ "$#" -gt 0 ]]; then
  echo "qwen-build-adapter: unexpected argument: $1 (the prompt is read from stdin)" >&2
  exit 2
fi

prompt="$(cat)"

# shellcheck disable=SC2206
qwen_flags=(${QWEN_FLAGS_STRING})

exec "${QWEN_BIN}" "${qwen_flags[@]}" -p "${prompt}"
