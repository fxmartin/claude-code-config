#!/usr/bin/env bats
# Tests for scripts/compute-release.sh (Story 5.2-001).
#
# Strategy: feed the bumper a current version plus a NUL-separated stream of
# commit messages on stdin and assert the computed BUMP / VERSION lines. The
# script is the single source of semver truth for the release workflow, so the
# Conventional Commit bump rules are pinned here.

BUMPER="${BATS_TEST_DIRNAME}/../scripts/compute-release.sh"

# Emit commit messages as a newline-separated stream. The bumper scans stdin
# line by line and is agnostic to the record boundary, so this faithfully
# models a `git log` dump where commit headers each sit on their own line.
commits() {
    local msg
    for msg in "$@"; do
        printf '%s\n' "${msg}"
    done
}

@test "feat commit bumps MINOR" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'feat: add release workflow')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=minor"* ]]
    [[ "${output}" == *"VERSION=v1.4.0"* ]]
}

@test "fix commit bumps PATCH" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'fix: correct a typo')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=patch"* ]]
    [[ "${output}" == *"VERSION=v1.3.1"* ]]
}

@test "perf and refactor commits bump PATCH" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'perf: speed up scan' 'refactor: tidy helper')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=patch"* ]]
    [[ "${output}" == *"VERSION=v1.3.1"* ]]
}

@test "BREAKING CHANGE footer bumps MAJOR" {
    run "${BUMPER}" v1.3.0 <<<"$(commits $'feat: rework api\n\nBREAKING CHANGE: removed old flag')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=major"* ]]
    [[ "${output}" == *"VERSION=v2.0.0"* ]]
}

@test "bang after type bumps MAJOR" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'feat!: drop legacy hook')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=major"* ]]
    [[ "${output}" == *"VERSION=v2.0.0"* ]]
}

@test "bang after scoped type bumps MAJOR" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'fix(cmux)!: drop stdin')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=major"* ]]
}

@test "feat outranks fix — MINOR wins over PATCH" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'fix: a' 'feat: b' 'fix: c')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=minor"* ]]
    [[ "${output}" == *"VERSION=v1.4.0"* ]]
}

@test "breaking outranks feat — MAJOR wins" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'feat: a' 'feat!: b')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=major"* ]]
}

@test "only chore/docs/test/ci/build commits produce no release" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'docs: tidy readme' 'chore: bump deps' 'ci: tweak workflow' 'test: add case' 'build: pin tool')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=none"* ]]
    [[ "${output}" == *"no release"* ]]
}

@test "empty commit stream produces no release" {
    run "${BUMPER}" v1.3.0 </dev/null
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=none"* ]]
}

@test "missing v prefix on current version is accepted" {
    run "${BUMPER}" 1.3.0 <<<"$(commits 'feat: x')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"VERSION=v1.4.0"* ]]
}

@test "scoped feat is recognised" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'feat(release): scoped feature')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=minor"* ]]
}

@test "minor bump zeroes the patch component" {
    run "${BUMPER}" v1.3.7 <<<"$(commits 'feat: x')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"VERSION=v1.4.0"* ]]
}

@test "major bump zeroes minor and patch" {
    run "${BUMPER}" v1.3.7 <<<"$(commits 'feat!: x')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"VERSION=v2.0.0"* ]]
}

@test "from v0.0.0 a feat produces v0.1.0" {
    run "${BUMPER}" v0.0.0 <<<"$(commits 'feat: first feature')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"VERSION=v0.1.0"* ]]
}

@test "non-conventional commit subjects are ignored" {
    run "${BUMPER}" v1.3.0 <<<"$(commits 'Merge branch main' 'WIP random text')"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"BUMP=none"* ]]
}

@test "malformed current version is rejected with exit 2" {
    run "${BUMPER}" "not-a-version" <<<"$(commits 'feat: x')"
    [ "${status}" -eq 2 ]
}

@test "missing current-version argument is rejected with exit 2" {
    run "${BUMPER}" </dev/null
    [ "${status}" -eq 2 ]
}
