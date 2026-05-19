#!/usr/bin/env bats
# Tests for .commitlintrc.json — verifies that the conventional-commits config
# accepts the framework's own commit styles and rejects non-conventional messages.
#
# Requires: @commitlint/cli and @commitlint/config-conventional installed locally
# (e.g. via `npm install --no-save @commitlint/cli @commitlint/config-conventional`).

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
COMMITLINT="${REPO_ROOT}/node_modules/.bin/commitlint"

# ---------------------------------------------------------------------------
# Accept cases — the framework's own commit styles must all pass
# ---------------------------------------------------------------------------

@test "accepts feat(scope): lowercase subject" {
  echo "feat(release): conventional commits + commitlint on prs" \
    | "$COMMITLINT"
}

@test "accepts docs: no scope, lowercase subject" {
  echo "docs: mark story x as done" \
    | "$COMMITLINT"
}

@test "accepts test(scope): lowercase subject" {
  echo "test(release): add coverage for conventional commits" \
    | "$COMMITLINT"
}

@test "accepts chore(scope): lowercase subject with version" {
  echo "chore(release): align manifests to v1.3.0" \
    | "$COMMITLINT"
}

@test "accepts feat: no scope, lowercase subject" {
  echo "feat: add rate-limit segment to statusline" \
    | "$COMMITLINT"
}

# ---------------------------------------------------------------------------
# Reject cases — non-conventional and case-violating messages must be rejected
# ---------------------------------------------------------------------------

@test "rejects bare sentence with no type" {
  run bash -c "echo 'fixed stuff' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}

@test "rejects sentence-case subject (leading capital)" {
  run bash -c "echo 'feat: Add something' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}

@test "rejects all-uppercase subject" {
  run bash -c "echo 'feat: ADD SOMETHING' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}

@test "rejects PascalCase subject" {
  run bash -c "echo 'feat: AddSomething' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}

@test "rejects subject with trailing period" {
  run bash -c "echo 'feat: add something.' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}

@test "rejects unknown type (not in type-enum)" {
  run bash -c "echo 'wip: add something' | '$COMMITLINT'"
  [ "$status" -ne 0 ]
}
