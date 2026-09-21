#!/usr/bin/env bats
# ABOUTME: CLAUDE.md and AGENTS.md must describe the same machines
# ABOUTME: They are symlinked onto every host, so a drifting table misleads one

setup() {
    REPO_ROOT="${BATS_TEST_DIRNAME}/.."
    CLAUDE_MD="${REPO_ROOT}/CLAUDE.md"
    AGENTS_MD="${REPO_ROOT}/AGENTS.md"
}

@test "both instruction files carry a Machines section" {
    run rg -n '^## Machines$' "$CLAUDE_MD"
    [ "$status" -eq 0 ]
    run rg -n '^## Machines$' "$AGENTS_MD"
    [ "$status" -eq 0 ]
}

@test "the Machines section is identical in both files" {
    # Extract from the heading to the next top-level heading, exclusive.
    claude_block="$(awk '/^## Machines$/{f=1} f&&/^## /&&!/^## Machines$/{exit} f' "$CLAUDE_MD")"
    agents_block="$(awk '/^## Machines$/{f=1} f&&/^## /&&!/^## Machines$/{exit} f' "$AGENTS_MD")"
    [ -n "$claude_block" ]
    [ "$claude_block" = "$agents_block" ]
}

@test "the Machines section names both hosts" {
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'macbook-pro-m3-max' "$f"
        [ "$status" -eq 0 ]
        run rg -n 'Dell XPS 13 9350' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "the Machines section tells the agent to detect the platform" {
    # A shared config that asserts one platform is wrong on the other host.
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'uname -s' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "omarchy command discovery is a command, not a hardcoded list" {
    # Omarchy ships new omarchy-* scripts per release; a frozen list goes stale.
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'compgen -c omarchy' "$f"
        [ "$status" -eq 0 ]
    done
}
