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
    BUILD_PY="${REPO_ROOT}/controller/src/sdlc/build.py"
    CLI_PY="${REPO_ROOT}/controller/src/sdlc/cli.py"
    CONTROLLER_SRC="${REPO_ROOT}/controller/src"
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

@test "the section names each command and marker it relies on" {
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

@test "DEAD is read from sdlc runs, the view that derives it" {
    # status_snapshot never calls derive_state: a crashed run stays IN_PROGRESS
    # there, so an agent waiting on sdlc status for DEAD waits forever.
    rule="$(piloting_block "$CLAUDE_MD" | grep 'One live run per repo')"
    [[ "$rule" == *'`sdlc runs` shows `DEAD`'* ]]
    [[ "$rule" != *'`sdlc status` shows'* ]]
    # -eq 1, not -ne 0: grep exits 2 on a moved file and would pass unchecked.
    run grep -n 'sdlc status.*DEAD' "$CLI_PY"
    [ "$status" -eq 1 ]
}

@test "a dead run needs no --force" {
    # find_live_owner already skips a gone pid, so --force is for a refusal.
    rule="$(piloting_block "$CLAUDE_MD" | grep 'One live run per repo')"
    [[ "$rule" == *'plain `sdlc resume`'* ]]
    [[ "$rule" == *'only after a refusal'* ]]
}

@test "the section admits a fresh sdlc build has no live-owner guard" {
    rule="$(piloting_block "$CLAUDE_MD" | grep 'One live run per repo')"
    [[ "$rule" == *'a fresh `sdlc build` is not guarded'* ]]
    # When build grows the refusal this fails: drop the caveat from the rule.
    run grep -n 'format_live_owner_refusal' "$BUILD_PY"
    [ "$status" -eq 1 ]
}

@test "the section does not promise enforcement the controller lacks" {
    # Most rules are conventions; calling them guard rails has an agent rely on
    # a refusal that never comes. Applies to the guide's summary too.
    block="$(piloting_block "$CLAUDE_MD")"
    [[ "$block" != *'guard rail'* ]]
    [[ "$block" == *'conventions the controller does not enforce'* ]]
    section="$(awk '/^### Section 4b:/{f=1;next} f&&/^### /{exit} f' "$GUIDE_MD")"
    [ -n "$section" ]
    [[ "$section" != *'guard rail'* ]]
}

@test "the nesting rule admits SDLC_BATCH_BUILD is only a marker" {
    rule="$(piloting_block "$CLAUDE_MD" | grep 'Never nest')"
    [[ "$rule" == *'the controller will not refuse a nested run'* ]]
    # When the controller reads the variable this fails: drop the caveat. Only
    # dispatch's assignment and its docstring may mention it.
    [ -d "$CONTROLLER_SRC" ]
    readers="$(grep -rn --include='*.py' 'SDLC_BATCH_BUILD' "$CONTROLLER_SRC" |
        grep -v -e 'env\["SDLC_BATCH_BUILD"\] = "1"' -e '``SDLC_BATCH_BUILD=1``' || true)"
    [ -z "$readers" ]
}

@test "the look-first commands are not overclaimed as read-only" {
    # sdlc status runs ledger.ensure_migrated() first, so it can write schema.
    rule="$(piloting_block "$CLAUDE_MD" | grep 'Look before acting')"
    [[ "$rule" != *'read-only'* ]]
    [[ "$rule" == *'change no run state'* ]]
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
