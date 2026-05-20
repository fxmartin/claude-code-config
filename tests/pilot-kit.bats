#!/usr/bin/env bats
# Story 6.3-001 — assertions for the LTM colleague pilot kit.
#
# Scope reminder: this story's deliverable is the KIT, not the pilot itself.
# The pilot has to be run by FX with five real LTM colleagues, so these tests
# only verify the artifacts exist, are structured correctly, and cross-link
# to the onboarding doc (Story 6.1-001) that colleagues will read first.

REPO_ROOT="${BATS_TEST_DIRNAME}/.."

@test "docs/pilot-kit/README.md exists and is non-empty" {
    local doc="$REPO_ROOT/docs/pilot-kit/README.md"
    [ -f "$doc" ]
    [ -s "$doc" ]
}

@test "docs/pilot-kit/feedback-template.md has the required sections" {
    local doc="$REPO_ROOT/docs/pilot-kit/feedback-template.md"
    [ -f "$doc" ]
    # Structured-form sections a colleague fills in after their run.
    grep -qiE "[Ii]nstall time"          "$doc"
    grep -qiE "[Bb]locker"               "$doc"
    grep -qiE "[Ww]hat worked"           "$doc"
    grep -qiE "[Ww]hat ?did ?n.t work"   "$doc"
    # 1-5 recommendation score
    grep -qiE "1.{0,3}(-|to).{0,3}5|score" "$doc"
    grep -qiE "[Rr]ecommend"             "$doc"
}

@test "docs/pilot-kit/decision-record.md has pass/fail checklist and go/no-go" {
    local doc="$REPO_ROOT/docs/pilot-kit/decision-record.md"
    [ -f "$doc" ]
    # The pass/fail criteria (from epic-06 acceptance: >= 4/5 yes-or-better)
    grep -qiE "pass.?fail|pass / fail|acceptance criteria" "$doc"
    # Explicit go/no-go decision section
    grep -qiE "go.?no.?go|go / no.go|decision"             "$doc"
    # Must be an EMPTY form — no fabricated pilot results
    ! grep -qiE "verdict.*:.*(yes|no|pass|fail|ship)\b"    "$doc" || \
        ( echo "decision-record.md contains a pre-filled verdict; must remain blank" && false )
}

@test "docs/pilot-kit/pilot-tracker.md exists with per-colleague rows" {
    local doc="$REPO_ROOT/docs/pilot-kit/pilot-tracker.md"
    [ -f "$doc" ]
    # Tracker columns: install date, build-stories date, feedback returned, issues
    grep -qiE "[Ii]nstall.*date"           "$doc"
    grep -qiE "build.stories|build stories" "$doc"
    grep -qiE "[Ff]eedback"                "$doc"
    grep -qiE "[Ii]ssues?"                 "$doc"
}

@test "every pilot-kit doc cross-links to docs/onboarding.md" {
    for doc in \
        "$REPO_ROOT/docs/pilot-kit/README.md" \
        "$REPO_ROOT/docs/pilot-kit/feedback-template.md" \
        "$REPO_ROOT/docs/pilot-kit/decision-record.md" \
        "$REPO_ROOT/docs/pilot-kit/pilot-tracker.md"
    do
        [ -f "$doc" ]
        grep -qF "onboarding.md" "$doc"
    done
}

@test "README.md links to docs/pilot-kit/README.md" {
    grep -qF "docs/pilot-kit/README.md" "$REPO_ROOT/README.md"
}

@test "scripts/pilot-helper.sh exists, is executable, and prints a markdown block" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    [ -f "$script" ]
    [ -x "$script" ]
    # Help flag must succeed (smoke check, no interactive prompts).
    run "$script" --help
    [ "$status" -eq 0 ]
    # Non-interactive env-capture mode (no questions asked) must emit markdown.
    PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    # The output is a paste-ready markdown block colleagues drop into the form.
    echo "$output" | grep -qF "## Environment"
    echo "$output" | grep -qiE "OS|operating system"
    echo "$output" | grep -qiE "shell"
}
