#!/usr/bin/env bats
# ABOUTME: CLAUDE.md and AGENTS.md must carry the same "Piloting the SDLC" section
# ABOUTME: Both are symlinked onto every host; a drifting copy misleads one harness

setup() {
    REPO_ROOT="${BATS_TEST_DIRNAME}/.."
    CLAUDE_MD="${REPO_ROOT}/CLAUDE.md"
    AGENTS_MD="${REPO_ROOT}/AGENTS.md"
}

# Extract from the heading to the next top-level heading, exclusive.
_section() {
    awk '/^## Piloting the SDLC$/{f=1} f&&/^## /&&!/^## Piloting the SDLC$/{exit} f' "$1"
}

@test "both instruction files carry a Piloting the SDLC section" {
    run grep -n '^## Piloting the SDLC$' "$CLAUDE_MD"
    [ "$status" -eq 0 ]
    run grep -n '^## Piloting the SDLC$' "$AGENTS_MD"
    [ "$status" -eq 0 ]
}

@test "the Piloting the SDLC section is identical in both files" {
    claude_block="$(_section "$CLAUDE_MD")"
    agents_block="$(_section "$AGENTS_MD")"
    [ -n "$claude_block" ]
    [ "$claude_block" = "$agents_block" ]
}

@test "the section names the dispatched-agent marker and the result contract" {
    # SDLC_BATCH_BUILD is what dispatch.py exports to every agent it spawns; an
    # agent that sees it must never start a nested run.
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        block="$(_section "$f")"
        [[ "$block" == *"SDLC_BATCH_BUILD"* ]]
        [[ "$block" == *"<<<RESULT_JSON>>>"* ]]
    done
}

@test "the section lists the read-only commands with --json" {
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        block="$(_section "$f")"
        for cmd in "sdlc status --json" "sdlc runs --json" "sdlc queue list --json" "sdlc doctor --json"; do
            [[ "$block" == *"$cmd"* ]]
        done
    done
}

@test "the section stays short enough to be weighed (bloat guard)" {
    # docs/claude-md-guide.md: past ~150 lines the model weighs each rule less.
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        lines="$(_section "$f" | wc -l | tr -d ' ')"
        [ "$lines" -le 20 ]
    done
}
