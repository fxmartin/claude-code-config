#!/usr/bin/env bats
# ABOUTME: Story 26.2-002 — reviewers treat the implementer's report as unverified claims.
# ABOUTME: Prompt-content assertions so the distrust hardening cannot be silently dropped.
#
# Every prompt surface that hands an implementer's self-report to a reviewer
# must carry two instructions (pattern source: superpowers task-reviewer-prompt):
#   1. Distrust: implementer claims — including design rationales like
#      "kept it simple per YAGNI" — are unverified until checked against the diff.
#   2. Bounded exploration: inspect outside the diff only for a concrete named
#      risk, and name both the risk and what was checked in the report.
#
# Surfaces under test (survey result for this story):
#   - scripts/codex-adversarial-review.sh   (adversarial reviewer prompt)
#   - controller/src/sdlc/build.py          (pipeline review-stage prompt;
#     rendered-string behaviour is asserted in controller/tests/test_build.py,
#     the source-text anchor here keeps the bats gate self-contained)
#   - skills/fix-github-issue/review-gate-prompt.md
#   - plugins/autonomous-sdlc/skills/fix-issue/review-gate-prompt.md

REPO_ROOT="${BATS_TEST_DIRNAME}/.."

# assert_distrust_markers <file> — the invariant phrases every reviewer prompt
# surface must contain, verbatim (case-insensitive for the prose lead-in).
assert_distrust_markers() {
    local file="$1"
    [ -f "$file" ]
    grep -qi "do not trust" "$file"
    grep -qF "unverified claims" "$file"
    grep -qF "kept it simple per YAGNI" "$file"
    grep -qF "concrete named risk" "$file"
}

@test "codex adversarial review prompt distrusts the implementer's report" {
    assert_distrust_markers "$REPO_ROOT/scripts/codex-adversarial-review.sh"
}

@test "pipeline review-stage prompt (build.py) distrusts the implementer's report" {
    assert_distrust_markers "$REPO_ROOT/controller/src/sdlc/build.py"
}

@test "fix-github-issue review gate prompt distrusts the implementer's report" {
    assert_distrust_markers "$REPO_ROOT/skills/fix-github-issue/review-gate-prompt.md"
}

@test "autonomous-sdlc plugin review gate prompt distrusts the implementer's report" {
    assert_distrust_markers "$REPO_ROOT/plugins/autonomous-sdlc/skills/fix-issue/review-gate-prompt.md"
}

@test "skill and plugin review gate prompts stay byte-identical" {
    diff "$REPO_ROOT/skills/fix-github-issue/review-gate-prompt.md" \
         "$REPO_ROOT/plugins/autonomous-sdlc/skills/fix-issue/review-gate-prompt.md"
}
