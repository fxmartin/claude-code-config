#!/usr/bin/env bats
# Story 6.1-001 — cross-reference assertions for docs/onboarding.md.
#
# This is a docs-only story (no behaviour to instrument). These tests anchor
# the onboarding guide in the build by verifying it exists, that the README
# links to it from the install section, and that the doc references the four
# load-bearing concepts a new LTM colleague needs to know about:
#   - the modal installer flags (--core / --tools / --mcp / --shell / --all)
#   - the Conventional Commits requirement enforced in CI
#   - cmux and Telegram as OPTIONAL integrations (macOS-only / always-on)

REPO_ROOT="${BATS_TEST_DIRNAME}/.."

@test "docs/onboarding.md exists and is non-empty" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    [ -f "$doc" ]
    [ -s "$doc" ]
}

@test "README.md links to docs/onboarding.md" {
    grep -qF "docs/onboarding.md" "$REPO_ROOT/README.md"
}

@test "docs/onboarding.md references the modal installer flags" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    grep -qF -- "--core" "$doc"
    grep -qF -- "--tools" "$doc"
    grep -qF -- "--mcp" "$doc"
    grep -qF -- "--shell" "$doc"
    grep -qF -- "--all" "$doc"
}

@test "docs/onboarding.md references the Conventional Commits requirement" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    grep -qi "Conventional Commits" "$doc"
}

@test "docs/onboarding.md lists cmux and Telegram as optional" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    grep -qi "cmux" "$doc"
    grep -qi "Telegram" "$doc"
    # The "Optional integrations" section must exist as a header.
    grep -qE "^#+ .*[Oo]ptional integrations" "$doc"
}
