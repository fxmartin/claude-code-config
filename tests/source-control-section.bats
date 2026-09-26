#!/usr/bin/env bats
# ABOUTME: CLAUDE.md and AGENTS.md must state the same source-control topology
# ABOUTME: Local GitLab is master; a drifting rule in one file sends an agent to the wrong forge

setup() {
    REPO_ROOT="${BATS_TEST_DIRNAME}/.."
    CLAUDE_MD="${REPO_ROOT}/CLAUDE.md"
    AGENTS_MD="${REPO_ROOT}/AGENTS.md"
    SC_REF="${REPO_ROOT}/reference-docs/source-control.md"
}

@test "both instruction files carry a Source Control section" {
    run rg -n '^## Source Control' "$CLAUDE_MD"
    [ "$status" -eq 0 ]
    run rg -n '^## Source Control' "$AGENTS_MD"
    [ "$status" -eq 0 ]
}

@test "the Source Control section is identical in both files" {
    # Extract from the heading to the next top-level heading, exclusive.
    claude_block="$(awk '/^## Source Control/{f=1;next} f&&/^## /{exit} f' "$CLAUDE_MD")"
    agents_block="$(awk '/^## Source Control/{f=1;next} f&&/^## /{exit} f' "$AGENTS_MD")"
    [ -n "$claude_block" ]
    [ "$claude_block" = "$agents_block" ]
}

@test "the section names gitlab.test as the way to reach GitLab" {
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'http://gitlab\.test' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "the section forbids merging on GitHub" {
    # Mirrors run keep_divergent_refs: a GitHub-side merge strands GitLab silently.
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n -i 'never merge on GitHub' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "the section names both GitHub-master exceptions" {
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'nix-install' "$f"
        [ "$status" -eq 0 ]
        run rg -n 'claude-code-config' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "the section keeps the GitHub MCP prohibition" {
    for f in "$CLAUDE_MD" "$AGENTS_MD"; do
        run rg -n 'Do NOT rely on a GitHub MCP server' "$f"
        [ "$status" -eq 0 ]
    done
}

@test "the source-control reference no longer routes PRs through gh by default" {
    # The reference is @-imported into every session; it must not contradict the section.
    run rg -n 'Push and create PR via `gh pr create`' "$SC_REF"
    [ "$status" -ne 0 ]
    run rg -n '^## GitLab CLI' "$SC_REF"
    [ "$status" -eq 0 ]
}
