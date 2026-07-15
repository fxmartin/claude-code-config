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

@test "batch-mode.md preserves the prose moved out of the core" {
    # The split must be lossless: the batch semantics that left SKILL.md have
    # to survive in batch-mode.md, not just the bare flag names.
    grep -qi 'bugs first' "${BATCH_MODE}"          # `all` ordering
    grep -q 'defaults to 1' "${BATCH_MODE}"        # `next` --limit default
    grep -qi 'overlapping files' "${BATCH_MODE}"   # investigate-first scheduling
    grep -q 'issues-all' "${BATCH_MODE}"           # batch ledger scope
    grep -qi 'doc-update' "${BATCH_MODE}"          # batch doc-update phase
    grep -qi 'never affects' "${BATCH_MODE}"       # doc-update is non-blocking
    grep -q 'ABOUTME' "${BATCH_MODE}"              # repo file-header convention
}

@test "moved scheduling prose does not creep back into the core body" {
    for phrase in 'bugs first' 'overlapping files' 'investigates every issue'; do
        if skill_body "${FIX_ISSUE_SKILL}" | grep -qi "$phrase"; then
            echo "core SKILL.md body re-documents batch prose: ${phrase}" >&2
            return 1
        fi
    done
}

@test "batch-mode reference uses the runtime-resolvable CLAUDE_SKILL_DIR form" {
    # A bare relative path would not resolve from an installed plugin.
    grep -qF '${CLAUDE_SKILL_DIR}/batch-mode.md' "${FIX_ISSUE_SKILL}"
}

@test "fix-issue frontmatter argument-hint still advertises the batch flags" {
    # The split's deal: prose moves out, flags stay discoverable in the hint.
    local fm
    fm="$(awk '/^---$/{n++; next} n==1{print}' "${FIX_ISSUE_SKILL}")"
    for flag in '--limit' '--sequential' '--concurrency'; do
        echo "$fm" | grep -q -- "$flag" || {
            echo "argument-hint lost ${flag}" >&2
            return 1
        }
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

@test "shared snippet states the full contract rules" {
    # The snippet is now the single source of the contract — losing a rule
    # here silently loses it for every notifying skill at once.
    grep -qF '~/.claude/hooks/notify-telegram.sh' "${SHARED_SNIPPET}"
    grep -qi 'unconditionally' "${SHARED_SNIPPET}"
    grep -qi 'best-effort' "${SHARED_SNIPPET}"
    grep -qi 'never block' "${SHARED_SNIPPET}"
    grep -q 'ABOUTME' "${SHARED_SNIPPET}"
}

@test "every notifying skill references the shared snippet" {
    for skill in "${NOTIFYING_SKILLS[@]}"; do
        grep -q '_shared/notifications\.md' "${SKILLS_DIR}/${skill}/SKILL.md" || {
            echo "${skill}/SKILL.md does not reference _shared/notifications.md" >&2
            return 1
        }
    done
}

@test "shared-snippet references use the runtime-resolvable CLAUDE_PLUGIN_ROOT form" {
    for skill in "${NOTIFYING_SKILLS[@]}"; do
        grep -qF '${CLAUDE_PLUGIN_ROOT}/skills/_shared/notifications.md' \
            "${SKILLS_DIR}/${skill}/SKILL.md" || {
            echo "${skill}/SKILL.md reference is not the CLAUDE_PLUGIN_ROOT form" >&2
            return 1
        }
    done
}

@test "every notifying skill still marks at least one milestone call" {
    # The blockquote promises pings "at the milestones marked below" — an
    # empty body below would break the contract without failing any test.
    for skill in "${NOTIFYING_SKILLS[@]}"; do
        skill_body "${SKILLS_DIR}/${skill}/SKILL.md" | grep -q 'notify-telegram\.sh' || {
            echo "${skill}/SKILL.md lost its milestone notify-telegram.sh call" >&2
            return 1
        }
    done
}

@test "notification boilerplate is not duplicated across skills" {
    # Each full boilerplate sentence must live in exactly one file: the snippet.
    local phrase carriers
    for phrase in \
        'There are no sidebar or desktop notifications' \
        'silent no-op when unconfigured'; do
        carriers="$(grep -rl "$phrase" "${SKILLS_DIR}" | sort)"
        [ "$carriers" = "${SHARED_SNIPPET}" ] || {
            echo "boilerplate '${phrase}' found outside the shared snippet:" >&2
            echo "$carriers" >&2
            return 1
        }
    done
}

# ─── CI wiring ───────────────────────────────────────────────────────────────

@test "ci.yml bats job runs the fix-issue skill-split suite" {
    grep -q 'tests/fix-issue-skill-split\.bats' "${REPO_ROOT}/.github/workflows/ci.yml"
}
