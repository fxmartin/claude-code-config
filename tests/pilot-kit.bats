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

# ---------------------------------------------------------------------------
# Gap-fill tests added by coverage gate (story 6.3-001)
# ---------------------------------------------------------------------------

@test "pilot-helper.sh: non-interactive mode is deterministic (no prompts)" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    # Run twice; both outputs must be stable (same structure, not same timestamp).
    PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    local first_output="$output"
    PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    # Both runs must contain the mandatory section header.
    echo "$first_output" | grep -qF "## Environment"
    echo "$output"       | grep -qF "## Environment"
    # Install path must read "(not provided)" when neither env-var is set
    # and NONINTERACTIVE is active — confirms no prompt was issued.
    echo "$first_output" | grep -qiF "(not provided)"
}

@test "pilot-helper.sh: missing gh prints 'not installed', exits 0" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    # Shadow gh with a command that doesn't exist by restricting PATH.
    PILOT_HELPER_NONINTERACTIVE=1 PATH="/usr/bin:/bin" run "$script"
    [ "$status" -eq 0 ]
    # gh is not on the stripped PATH — safe_version must report "not installed".
    echo "$output" | grep -qiF "not installed"
}

@test "pilot-helper.sh: PILOT_HELPER_INSTALL_PATH=A is accepted and printed" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    PILOT_HELPER_INSTALL_PATH=A PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    # The install-path line must reflect the A variant.
    echo "$output" | grep -qiE "marketplace|path.*A|A.*marketplace"
}

@test "pilot-helper.sh: PILOT_HELPER_INSTALL_PATH=B is accepted and printed" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    PILOT_HELPER_INSTALL_PATH=B PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    echo "$output" | grep -qiE "install\.sh|path.*B|B.*install"
}

@test "pilot-helper.sh: invalid PILOT_HELPER_INSTALL_PATH falls back gracefully" {
    local script="$REPO_ROOT/scripts/pilot-helper.sh"
    # An invalid value (e.g. "Z") must not crash the script.
    PILOT_HELPER_INSTALL_PATH=Z PILOT_HELPER_NONINTERACTIVE=1 run "$script"
    [ "$status" -eq 0 ]
    # Output is still a valid markdown block.
    echo "$output" | grep -qF "## Environment"
}

@test "feedback-template.md has no pre-populated sample answers" {
    local doc="$REPO_ROOT/docs/pilot-kit/feedback-template.md"
    # Sample-answer patterns: filled-in numeric times, real colleague names
    # embedded in answer fields, or verdicts already ticked. The form must be
    # blank so pilots give honest responses.
    # Check that none of the blank minute fields are pre-filled with a number.
    ! grep -qE "^- \*\*[A-Za-z ]+:\*\* [0-9]+ minutes" "$doc" || \
        ( echo "feedback-template.md has a pre-filled time answer; must remain blank" && false )
    # Score field must show a blank or placeholder, not a digit 1-5 pre-selected.
    # The "Score: _ / 5" line is the placeholder; a real score would be "Score: 4 / 5".
    ! grep -qE "^Score: [1-5] / 5" "$doc" || \
        ( echo "feedback-template.md has a pre-filled score; must remain blank" && false )
}

@test "decision-record.md has a must-fix-before-public-release section" {
    local doc="$REPO_ROOT/docs/pilot-kit/decision-record.md"
    grep -qiE "must.fix|release.block|before.*public.*release|block.*release" "$doc"
}

@test "decision-record.md has a deferred/post-MVP section" {
    local doc="$REPO_ROOT/docs/pilot-kit/decision-record.md"
    grep -qiE "defer|post.?mvp|post.?release" "$doc"
}

@test "decision-record.md cross-links feedback-template.md" {
    local doc="$REPO_ROOT/docs/pilot-kit/decision-record.md"
    grep -qF "feedback-template.md" "$doc"
}

@test "pilot-tracker.md has exactly 5 data rows (one per pilot colleague)" {
    local doc="$REPO_ROOT/docs/pilot-kit/pilot-tracker.md"
    # Count table rows that start with "| 1 |" through "| 5 |" — the five
    # colleague slots. We match on the leading row-number cell.
    local row_count
    row_count=$(grep -cE "^\| [1-5] \|" "$doc")
    [ "$row_count" -eq 5 ]
}

@test "pilot-tracker.md has install-path and date columns" {
    local doc="$REPO_ROOT/docs/pilot-kit/pilot-tracker.md"
    grep -qiE "[Ii]nstall path|install.path"  "$doc"
    grep -qiE "[Ii]nstall.?date|[Dd]ate"      "$doc"
}

@test "docs/pilot-kit/README.md states the expected time commitment" {
    local doc="$REPO_ROOT/docs/pilot-kit/README.md"
    # The README should tell colleagues roughly how long the pilot takes
    # so they can plan. Accept minutes/hours phrasing.
    grep -qiE "[0-9]+ ?min(ute)?s?|time budget|time commitment|wall.clock" "$doc"
}
