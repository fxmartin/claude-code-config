#!/usr/bin/env bats
# ABOUTME: Tests for hooks/on-pr-merge-docs.sh — the PostToolUse doc-update hook
# ABOUTME: must be a no-op during controller batch builds (issue #214).

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    HOOK="$REPO_ROOT/hooks/on-pr-merge-docs.sh"
}

# A successful `gh pr merge` PostToolUse payload.
_merge_payload() {
    printf '%s' '{"tool_input":{"command":"gh pr merge 42 --squash"},"tool_response":{"exitCode":0,"stdout":"Merged PR #42"}}'
}

@test "emits the doc-update context when SDLC_BATCH_BUILD is unset" {
    run env -u SDLC_BATCH_BUILD bash -c "'$HOOK'" <<<"$(_merge_payload)"
    [ "$status" -eq 0 ]
    [[ "$output" == *"additionalContext"* ]]
    [[ "$output" == *"PR #42"* ]]
}

@test "is a silent no-op when SDLC_BATCH_BUILD=1 (controller batch build)" {
    run env SDLC_BATCH_BUILD=1 bash -c "'$HOOK'" <<<"$(_merge_payload)"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "is a no-op on a non-zero merge exit code" {
    payload='{"tool_input":{"command":"gh pr merge 42"},"tool_response":{"exitCode":1,"stdout":""}}'
    run env -u SDLC_BATCH_BUILD bash -c "'$HOOK'" <<<"$payload"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}
