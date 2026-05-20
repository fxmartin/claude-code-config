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

@test "docs/onboarding.md references /build-stories and /build-stories resume" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    # /build-stories skill must appear
    grep -qF "/build-stories" "$doc"
    # The resume sub-command (Story 4.3-001) must be explicit
    grep -qF "/build-stories resume" "$doc"
}

@test "docs/onboarding.md has a Prerequisites section enumerating Claude Code, gh CLI, git" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    # Section header
    grep -qE "^#+ .*[Pp]rerequisites" "$doc"
    # Three load-bearing prerequisites
    grep -qF "Claude Code" "$doc"
    grep -qE "gh.*(CLI|cli)" "$doc"
    grep -qE "^| \`git\`|git.*(configured|config)" "$doc"
}

@test "docs/onboarding.md explicitly states cmux is macOS-only" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    # Must clearly label cmux as macOS-only so a Windows/WSL2 colleague
    # does not think it is a hard requirement.
    grep -qiE "cmux.*(macOS.only|macOS-only)" "$doc"
}

@test "docs/onboarding.md lists Telegram as opt-in with a setup pointer" {
    local doc="$REPO_ROOT/docs/onboarding.md"
    # Telegram must be under the Optional integrations section (not required)
    grep -qE "^#+ .*[Oo]ptional integrations" "$doc"
    # Must reference the env-var names so a colleague knows where to look
    grep -qF "TELEGRAM_BOT_TOKEN" "$doc"
    grep -qF "TELEGRAM_CHAT_ID" "$doc"
    # Must reference .env so the colleague knows where to put the creds
    grep -qF ".env" "$doc"
}
