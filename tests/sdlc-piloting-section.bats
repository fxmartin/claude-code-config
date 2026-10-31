#!/usr/bin/env bats
# ABOUTME: CLAUDE.md and AGENTS.md must carry the same SDLC piloting rules
# ABOUTME: Agents otherwise only learn about the controller when a skill fires

setup() {
    REPO_ROOT="${BATS_TEST_DIRNAME}/.."
    CLAUDE_MD="${REPO_ROOT}/CLAUDE.md"
    AGENTS_MD="${REPO_ROOT}/AGENTS.md"
    CI_YML="${REPO_ROOT}/.github/workflows/ci.yml"
    GUIDE_MD="${REPO_ROOT}/docs/claude-md-guide.md"
    DISPATCH_PY="${REPO_ROOT}/controller/src/sdlc/dispatch.py"
}

# Extract from the heading to the next top-level heading, exclusive.
piloting_block() {
    awk '/^## Piloting the SDLC$/{f=1;print;next} f&&/^## /{exit} f' "$1"
}

@test "both instruction files carry a Piloting the SDLC section" {
    run grep -n '^## Piloting the SDLC$' "$CLAUDE_MD"
    [ "$status" -eq 0 ]
    run grep -n '^## Piloting the SDLC$' "$AGENTS_MD"
    [ "$status" -eq 0 ]
}

@test "the Piloting the SDLC section is identical in both files" {
    claude_block="$(piloting_block "$CLAUDE_MD")"
    agents_block="$(piloting_block "$AGENTS_MD")"
    [ -n "$claude_block" ]
    [ "$claude_block" = "$agents_block" ]
}

@test "the section states all five rules" {
    block="$(piloting_block "$CLAUDE_MD")"
    for marker in 'Look before acting' 'Front door only' 'Never nest' \
        'One live run per repo' 'Long runs are not tool calls'; do
        [[ "$block" == *"**$marker**"* ]]
    done
}

@test "the section names each guard rail it points at" {
    block="$(piloting_block "$CLAUDE_MD")"
    for marker in 'sdlc status' 'sdlc runs' 'sdlc queue list' 'sdlc doctor' \
        '--enqueue' 'run-*' 'SDLC_BATCH_BUILD' '<<<RESULT_JSON>>>' 'resume --force'; do
        [[ "$block" == *"$marker"* ]]
    done
}

@test "the section stays a pointer, not a manual" {
    # These files load into every session; detail belongs in the controller docs.
    line_count="$(piloting_block "$CLAUDE_MD" | wc -l)"
    [ "$line_count" -gt 0 ]
    [ "$line_count" -le 20 ]
}

@test "the nesting rule names the variable dispatch really sets" {
    # A renamed env var would leave "Never nest" silently unenforceable.
    run grep -n 'env\["SDLC_BATCH_BUILD"\]' "$DISPATCH_PY"
    [ "$status" -eq 0 ]
}

@test "both parity suites run in the CI bats list" {
    run grep -n 'tests/sdlc-piloting-section\.bats' "$CI_YML"
    [ "$status" -eq 0 ]
    run grep -n 'tests/machines-section\.bats' "$CI_YML"
    [ "$status" -eq 0 ]
}

@test "the CLAUDE.md guide documents the section" {
    run grep -n '^### Section 4b: Piloting the SDLC' "$GUIDE_MD"
    [ "$status" -eq 0 ]
}
