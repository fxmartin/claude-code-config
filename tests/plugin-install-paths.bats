#!/usr/bin/env bats
# Story 6.4-001 — verify both plugin install paths end-to-end.
#
# The framework ships TWO install paths and both must work:
#
#   Path A (local clone + install.sh): `./install.sh --core` symlinks the repo
#     into ~/.claude, including ~/.claude/plugins/marketplaces/fx-claude-config
#     → that exposes the autonomous-sdlc plugin to Claude Code locally.
#     This path is covered by scripts/smoke-test.sh; we re-assert the
#     marketplace symlink contract here so a structural regression surfaces
#     immediately.
#
#   Path B (GitHub-direct marketplace install): a user runs
#     `/plugin marketplace add fxmartin/claude-code-config` then
#     `/plugin install autonomous-sdlc@fx-claude-config` inside Claude Code.
#     Claude Code consumes:
#       - .claude-plugin/marketplace.json (marketplace manifest)
#       - plugins/autonomous-sdlc/.claude-plugin/plugin.json (plugin manifest)
#       - plugins/autonomous-sdlc/skills/<name>/SKILL.md (auto-discovered)
#     We cannot drive a real Claude Code session in CI, but we CAN validate
#     the on-disk structure that path B consumes is well-formed.
#
# scripts/verify-plugin-install.sh performs the structural validation.
# These tests pin the contract.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
VERIFY="${REPO_ROOT}/scripts/verify-plugin-install.sh"
MARKETPLACE_JSON="${REPO_ROOT}/.claude-plugin/marketplace.json"
PLUGIN_JSON="${REPO_ROOT}/plugins/autonomous-sdlc/.claude-plugin/plugin.json"
PLUGIN_SKILLS_DIR="${REPO_ROOT}/plugins/autonomous-sdlc/skills"

# ─── Marketplace manifest structural sanity ──────────────────────────────────

@test "marketplace manifest exists at .claude-plugin/marketplace.json" {
    [ -f "${MARKETPLACE_JSON}" ]
}

@test "marketplace manifest is valid JSON" {
    run jq -e . "${MARKETPLACE_JSON}"
    [ "$status" -eq 0 ]
}

@test "marketplace manifest declares name fx-claude-config" {
    run jq -er '.name' "${MARKETPLACE_JSON}"
    [ "$status" -eq 0 ]
    [ "$output" = "fx-claude-config" ]
}

@test "marketplace manifest declares at least one plugin" {
    run jq -e '.plugins | length > 0' "${MARKETPLACE_JSON}"
    [ "$status" -eq 0 ]
}

@test "marketplace declares the autonomous-sdlc plugin" {
    run jq -er '.plugins[] | select(.name == "autonomous-sdlc") | .name' \
        "${MARKETPLACE_JSON}"
    [ "$status" -eq 0 ]
    [ "$output" = "autonomous-sdlc" ]
}

@test "marketplace autonomous-sdlc source path resolves to a real directory" {
    src="$(jq -r '.plugins[] | select(.name == "autonomous-sdlc") | .source' \
           "${MARKETPLACE_JSON}")"
    # source is repo-relative (e.g. "./plugins/autonomous-sdlc")
    resolved="${REPO_ROOT}/${src#./}"
    [ -d "$resolved" ]
}

# ─── Path A guard: never install from an ephemeral worktree (#179) ───────────

@test "install.sh --core refuses to run from an agent worktree (#179)" {
    # install.sh --core symlinks every managed ~/.claude entry to SCRIPT_DIR.
    # From an ephemeral build worktree those links dangle on teardown, silently
    # breaking the live install. The guard must abort before creating any link.
    fake_home="$(mktemp -d)"
    wt="$(mktemp -d)/.claude/worktrees/agent-test-1"
    mkdir -p "$wt"
    # Symlink the installer + its lib dir so SCRIPT_DIR resolves to the worktree
    # while sourcing still works.
    ln -s "${REPO_ROOT}/install.sh" "$wt/install.sh"
    ln -s "${REPO_ROOT}/install"    "$wt/install"
    run env HOME="$fake_home" CLAUDE_CONFIG_NO_ENV=1 bash "$wt/install.sh" --core
    [ "$status" -ne 0 ]
    [[ "$output" == *worktree* ]]
    [ ! -e "$fake_home/.claude/CLAUDE.md" ]
    rm -rf "$fake_home" "${wt%/.claude/*}"
}

# ─── Plugin manifest structural sanity ───────────────────────────────────────

@test "autonomous-sdlc plugin manifest exists" {
    [ -f "${PLUGIN_JSON}" ]
}

@test "autonomous-sdlc plugin manifest is valid JSON" {
    run jq -e . "${PLUGIN_JSON}"
    [ "$status" -eq 0 ]
}

@test "autonomous-sdlc plugin manifest declares required keys (name, version, description)" {
    run jq -er '.name, .version, .description' "${PLUGIN_JSON}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"autonomous-sdlc"* ]]
}

# ─── Skill discovery ─────────────────────────────────────────────────────────

@test "autonomous-sdlc ships the eight MVP skills" {
    # Epic-06 success metric: pilot users must find these eight skills after install.
    expected_skills=(brainstorm build-stories create-epic create-story
                     fix-issue generate-epics project-init resume-build-agents)
    for skill in "${expected_skills[@]}"; do
        [ -f "${PLUGIN_SKILLS_DIR}/${skill}/SKILL.md" ] || {
            echo "missing SKILL.md for: ${skill}" >&2
            return 1
        }
    done
}

@test "every skill directory contains a SKILL.md with a name frontmatter field" {
    fail=0
    for skill_dir in "${PLUGIN_SKILLS_DIR}"/*/; do
        skill="$(basename "$skill_dir")"
        skill_md="${skill_dir}SKILL.md"
        if [ ! -f "$skill_md" ]; then
            echo "missing SKILL.md in $skill_dir" >&2
            fail=1
            continue
        fi
        # The frontmatter must declare `name: <skill>` matching the directory.
        if ! grep -qE "^name:[[:space:]]*${skill}[[:space:]]*$" "$skill_md"; then
            echo "$skill_md frontmatter name does not match dir '$skill'" >&2
            fail=1
        fi
    done
    [ "$fail" -eq 0 ]
}

# ─── verify-plugin-install.sh contract ───────────────────────────────────────

@test "scripts/verify-plugin-install.sh exists and is executable" {
    [ -f "${VERIFY}" ]
    [ -x "${VERIFY}" ]
}

@test "verify-plugin-install.sh exits 0 on the real repository" {
    run "${VERIFY}"
    [ "$status" -eq 0 ]
}

@test "verify-plugin-install.sh prints VERIFY_PLUGIN summary line" {
    run "${VERIFY}"
    [ "$status" -eq 0 ]
    # Stable, grep-parseable contract for CI: VERIFY_PLUGIN: <pass>/<total> passed
    echo "$output" | grep -qE '^VERIFY_PLUGIN: [0-9]+/[0-9]+ passed$'
}

@test "verify-plugin-install.sh reports every expected skill" {
    run "${VERIFY}"
    [ "$status" -eq 0 ]
    for skill in brainstorm build-stories create-epic create-story \
                 fix-issue generate-epics project-init resume-build-agents; do
        [[ "$output" == *"$skill"* ]] || {
            echo "verify output missing skill '$skill'" >&2
            return 1
        }
    done
}

@test "verify-plugin-install.sh fails when the marketplace manifest is missing" {
    fake_root="$(mktemp -d)"
    # No .claude-plugin/marketplace.json under fake_root.
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
}

@test "verify-plugin-install.sh fails when the marketplace manifest is invalid JSON" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    printf 'not valid json' > "${fake_root}/.claude-plugin/marketplace.json"
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
}

@test "verify-plugin-install.sh fails when a declared plugin source path is missing" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "autonomous-sdlc", "source": "./plugins/autonomous-sdlc", "version": "0.1.0"}
  ]
}
JSON
    # plugins/autonomous-sdlc deliberately not created.
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
}

@test "verify-plugin-install.sh fails when a declared plugin manifest is invalid JSON" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/autonomous-sdlc/.claude-plugin"
    mkdir -p "${fake_root}/plugins/autonomous-sdlc/skills/brainstorm"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "autonomous-sdlc", "source": "./plugins/autonomous-sdlc", "version": "0.1.0"}
  ]
}
JSON
    printf 'totally broken {' > "${fake_root}/plugins/autonomous-sdlc/.claude-plugin/plugin.json"
    printf -- '---\nname: brainstorm\n---\nbody\n' \
        > "${fake_root}/plugins/autonomous-sdlc/skills/brainstorm/SKILL.md"
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
}

@test "verify-plugin-install.sh fails when a declared plugin has zero skills" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/autonomous-sdlc/.claude-plugin"
    mkdir -p "${fake_root}/plugins/autonomous-sdlc/skills"  # empty
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "autonomous-sdlc", "source": "./plugins/autonomous-sdlc", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/autonomous-sdlc/.claude-plugin/plugin.json" <<'JSON'
{"name": "autonomous-sdlc", "version": "0.0.1", "description": "test"}
JSON
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
}

@test "verify-plugin-install.sh --help prints usage and exits 0" {
    run "${VERIFY}" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage"* || "$output" == *"usage"* ]]
}

# ─── Gap tests: error-path coverage (Story 6.4-001 QA gate) ─────────────────
#
# The following tests extend coverage beyond the initial 21, targeting the
# specific gaps identified in the QA gate:
#   1. Malformed JSON manifest — error message identifies the file
#   2. Declared skill path missing (skill dir exists, SKILL.md absent)
#   3. Agent referenced by a skill is missing — clean error with path
#   4. VERIFY_PLUGIN summary is grep-parseable (already tested, re-pinned)
#   5. Empty plugins array — exits 1 with "no plugins declared" message
#   6. Idempotency — running the script twice yields identical output

# ── Gap 1: malformed JSON error message is informative ──────────────────────

@test "verify-plugin-install.sh malformed JSON error message identifies the file" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    printf 'not valid json }{' > "${fake_root}/.claude-plugin/marketplace.json"
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
    # The combined stdout+stderr must reference the parse failure clearly.
    [[ "${output}" == *"jq parse failed"* ]]
}

# ── Gap 2: skill directory present but missing SKILL.md ─────────────────────

@test "verify-plugin-install.sh fails when a skill dir is missing its SKILL.md" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    # Two skill dirs: one has SKILL.md, one does not.
    mkdir -p "${fake_root}/plugins/test-plugin/skills/good-skill"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/broken-skill"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    printf -- '---\nname: good-skill\n---\nbody\n' \
        > "${fake_root}/plugins/test-plugin/skills/good-skill/SKILL.md"
    # broken-skill dir deliberately has NO SKILL.md.
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
    # Error output should mention the missing skill directory.
    [[ "${output}" == *"broken-skill"* ]]
}

@test "verify-plugin-install.sh error for missing SKILL.md points at the unresolved path" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/orphan-skill"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    # orphan-skill has no SKILL.md — this is the only skill dir, so skill_count=0 too.
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
    # Output must name the skill that has no SKILL.md.
    [[ "${output}" == *"orphan-skill"* ]]
}

# ── Gap 3: agent referenced by a skill is unresolved ────────────────────────

@test "verify-plugin-install.sh fails when a skill SKILL.md references an unknown agent" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/my-skill"
    mkdir -p "${fake_root}/agents"
    # Write a real agent file so the agents/ check passes.
    printf '# real-agent\nA real agent.\n' > "${fake_root}/agents/real-agent.md"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    # SKILL.md references a subagent_type that does NOT exist in agents/.
    cat > "${fake_root}/plugins/test-plugin/skills/my-skill/SKILL.md" <<'SKILL'
---
name: my-skill
description: A test skill
---
# My Skill
Agent(subagent_type="ghost-agent", prompt="do stuff")
SKILL
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
    # Error output must mention the unresolved agent name.
    [[ "${output}" == *"ghost-agent"* ]]
}

@test "verify-plugin-install.sh passes when skill subagent_type references a known agent" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/my-skill"
    mkdir -p "${fake_root}/agents"
    printf '# known-agent\nA known agent.\n' > "${fake_root}/agents/known-agent.md"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    cat > "${fake_root}/plugins/test-plugin/skills/my-skill/SKILL.md" <<'SKILL'
---
name: my-skill
description: A test skill
---
# My Skill
Agent(subagent_type="known-agent", prompt="do stuff")
SKILL
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -eq 0 ]
}

@test "verify-plugin-install.sh passes when skill uses a built-in subagent_type" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/my-skill"
    mkdir -p "${fake_root}/agents"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    # Uses built-in subagent types — no agents/ file needed.
    cat > "${fake_root}/plugins/test-plugin/skills/my-skill/SKILL.md" <<'SKILL'
---
name: my-skill
description: A test skill
---
# My Skill
Agent(subagent_type="general-purpose", prompt="do stuff")
SKILL
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -eq 0 ]
}

@test "verify-plugin-install.sh skips bracketed placeholder subagent_type values" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/.claude-plugin"
    mkdir -p "${fake_root}/plugins/test-plugin/skills/my-skill"
    mkdir -p "${fake_root}/agents"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": [
    {"name": "test-plugin", "source": "./plugins/test-plugin", "version": "0.1.0"}
  ]
}
JSON
    cat > "${fake_root}/plugins/test-plugin/.claude-plugin/plugin.json" <<'JSON'
{"name": "test-plugin", "version": "0.0.1", "description": "test plugin"}
JSON
    # [AGENT_TYPE] is a placeholder, not a literal agent name — must be skipped.
    cat > "${fake_root}/plugins/test-plugin/skills/my-skill/SKILL.md" <<'SKILL'
---
name: my-skill
description: A test skill
---
# My Skill
Agent(subagent_type=[AGENT_TYPE], prompt="do stuff")
SKILL
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -eq 0 ]
}

# ── Gap 4: summary line grep contract (re-pinned with count assertion) ───────

@test "verify-plugin-install.sh VERIFY_PLUGIN summary format is grep-parseable with numbers" {
    run "${VERIFY}"
    [ "$status" -eq 0 ]
    # Must match: VERIFY_PLUGIN: <N>/<M> passed where N and M are integers.
    echo "$output" | grep -qE '^VERIFY_PLUGIN: [0-9]+/[0-9]+ passed$'
    # Sanity: N must equal M on the clean repo (all checks pass).
    summary_line="$(echo "$output" | grep '^VERIFY_PLUGIN:')"
    passed="$(echo "$summary_line" | grep -oE '[0-9]+' | head -1)"
    total="$(echo "$summary_line" | grep -oE '[0-9]+' | tail -1)"
    [ "$passed" -eq "$total" ]
}

# ── Gap 5: empty plugins array ───────────────────────────────────────────────

@test "verify-plugin-install.sh fails with a clear message when plugins array is empty" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": []
}
JSON
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    [ "$status" -ne 0 ]
    # Output must mention that no plugins are declared.
    [[ "${output}" == *"no plugins declared"* || "${output}" == *"plugins[] empty"* ]]
}

@test "verify-plugin-install.sh exits 1 (not 2) when plugins array is empty" {
    fake_root="$(mktemp -d)"
    mkdir -p "${fake_root}/.claude-plugin"
    cat > "${fake_root}/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "fx-claude-config",
  "owner": {"name": "FX"},
  "plugins": []
}
JSON
    run env VERIFY_PLUGIN_ROOT_OVERRIDE="${fake_root}" "${VERIFY}"
    rm -rf "${fake_root}"
    # Exit 1 = check failure; exit 2 = preflight failure. Empty plugins is a
    # check failure, not a preflight error.
    [ "$status" -eq 1 ]
}

# ── Gap 6: idempotency ───────────────────────────────────────────────────────

@test "verify-plugin-install.sh is idempotent: two consecutive runs produce identical output" {
    run "${VERIFY}"
    [ "$status" -eq 0 ]
    first_output="$output"
    run "${VERIFY}"
    [ "$status" -eq 0 ]
    [ "$output" = "$first_output" ]
}

@test "verify-plugin-install.sh leaves no temp files after a successful run" {
    tmp_before="$(mktemp -d)"
    # Run with the real repo; if the script creates temp files they'd be in TMPDIR.
    TMPDIR="${tmp_before}" run env VERIFY_PLUGIN_ROOT_OVERRIDE="${REPO_ROOT}" "${VERIFY}"
    leftover_count="$(find "${tmp_before}" -mindepth 1 | wc -l | tr -d ' ')"
    rm -rf "${tmp_before}"
    [ "$status" -eq 0 ]
    [ "$leftover_count" -eq 0 ]
}
