#!/usr/bin/env bats
# ABOUTME: Tests for Story 27.1-004 — fix-issue SKILL.md stays a lean core with
# the batch-mode reference split into batch-mode.md (loaded only for `all` /
# `next --limit=N` invocations), and the Telegram-notification boilerplate that
# was duplicated verbatim across the notifying skills lives in one shared
# snippet under skills/_shared/ referenced by each carrier.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
SKILLS_DIR="${REPO_ROOT}/plugins/autonomous-sdlc/skills"
FIX_ISSUE_SKILL="${SKILLS_DIR}/fix-issue/SKILL.md"
BATCH_MODE="${SKILLS_DIR}/fix-issue/batch-mode.md"
SHARED_SNIPPET="${SKILLS_DIR}/_shared/notifications.md"

# Skills that carried the identical notification-boilerplate blockquote.
# (fix-issue was the fifth carrier before the #436 controller migration
# removed its run-logging/notification prose entirely.)
NOTIFYING_SKILLS=(brainstorm create-epic create-story generate-epics)

# Strip the YAML frontmatter block (between the first two `---` lines) so
# body assertions don't trip on the argument-hint.
skill_body() {
    awk '/^---$/{n++; next} n>=2{print}' "$1"
}

# ─── Core / batch-mode split ─────────────────────────────────────────────────

@test "fix-issue SKILL.md stays at or under 1500 words" {
    local words
    words="$(wc -w < "${FIX_ISSUE_SKILL}")"
    if [ "$words" -gt 1500 ]; then
        echo "fix-issue SKILL.md is ${words} words (limit 1500)" >&2
        return 1
    fi
}

@test "fix-issue ships a batch-mode.md reference file" {
    [ -f "${BATCH_MODE}" ]
}

@test "fix-issue core instructs reading batch-mode.md only for batch targets" {
    # The core must point at batch-mode.md and gate that read on the batch
    # targets (`all` / `next` with `--limit=N`) in the same passage (the
    # prose wraps, so allow a one-line window around the reference).
    run grep -E 'batch-mode\.md' "${FIX_ISSUE_SKILL}"
    [ "$status" -eq 0 ]
    grep -E -B1 -A1 'batch-mode\.md' "${FIX_ISSUE_SKILL}" | grep -q '`all`' || {
        echo "batch-mode.md reference does not mention the \`all\` target" >&2
        return 1
    }
    grep -E -B1 -A1 'batch-mode\.md' "${FIX_ISSUE_SKILL}" | grep -q -- '--limit' || {
        echo "batch-mode.md reference does not mention --limit" >&2
        return 1
    }
}

@test "batch-mode.md documents every batch-only flag" {
    for flag in -- '--limit' '--sequential' '--concurrency'; do
        [ "$flag" = "--" ] && continue
        grep -q -- "$flag" "${BATCH_MODE}" || {
            echo "batch-mode.md missing ${flag}" >&2
            return 1
        }
    done
}

@test "batch-only flag prose lives in batch-mode.md, not the core body" {
    # argument-hint (frontmatter) may still list them; the body must not
    # re-document the batch-only flags the split moved out.
    for flag in '--sequential' '--concurrency'; do
        if skill_body "${FIX_ISSUE_SKILL}" | grep -q -- "$flag"; then
            echo "core SKILL.md body still documents ${flag}" >&2
            return 1
        fi
    done
}

# ─── Shared notification boilerplate ─────────────────────────────────────────

@test "shared notifications snippet exists under skills/_shared/" {
    [ -f "${SHARED_SNIPPET}" ]
}

@test "shared snippet carries the notify-telegram contract" {
    grep -q 'notify-telegram\.sh' "${SHARED_SNIPPET}"
    grep -qi 'no-op' "${SHARED_SNIPPET}"
}

@test "every notifying skill references the shared snippet" {
    for skill in "${NOTIFYING_SKILLS[@]}"; do
        grep -q '_shared/notifications\.md' "${SKILLS_DIR}/${skill}/SKILL.md" || {
            echo "${skill}/SKILL.md does not reference _shared/notifications.md" >&2
            return 1
        }
    done
}

@test "notification boilerplate is not duplicated across skills" {
    # The full boilerplate sentence must live in exactly one file: the snippet.
    local phrase='There are no sidebar or desktop notifications'
    local carriers
    carriers="$(grep -rl "$phrase" "${SKILLS_DIR}" | sort)"
    [ "$carriers" = "${SHARED_SNIPPET}" ] || {
        echo "boilerplate found outside the shared snippet:" >&2
        echo "$carriers" >&2
        return 1
    }
}
