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
