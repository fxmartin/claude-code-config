#!/usr/bin/env bats
# Tests for scope separation between plugin agents and personal agents
# (Story 6.2-001).
#
# Personal agents (crypto, profile research, executive summary) live in
# agents/personal/ so they are clearly excluded from the autonomous-sdlc
# plugin scope while still being installed by --core mode (the agents/
# directory is symlinked as a unit, so the subdirectory tags along).
#
# Plugin-scope agents stay directly under agents/.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
AGENTS_DIR="${REPO_ROOT}/agents"
PERSONAL_DIR="${AGENTS_DIR}/personal"
VALIDATOR="${REPO_ROOT}/scripts/validate-agent-registry.sh"

# --- Personal agents live under agents/personal/ ---------------------------

@test "agents/personal/ directory exists" {
    [ -d "${PERSONAL_DIR}" ]
}

@test "crypto-coin-analyzer.md is under agents/personal/" {
    [ -f "${PERSONAL_DIR}/crypto-coin-analyzer.md" ]
    [ ! -f "${AGENTS_DIR}/crypto-coin-analyzer.md" ]
}

@test "crypto-market-agent.md is under agents/personal/" {
    [ -f "${PERSONAL_DIR}/crypto-market-agent.md" ]
    [ ! -f "${AGENTS_DIR}/crypto-market-agent.md" ]
}

@test "executive-summary-generator.md is under agents/personal/" {
    [ -f "${PERSONAL_DIR}/executive-summary-generator.md" ]
    [ ! -f "${AGENTS_DIR}/executive-summary-generator.md" ]
}

@test "professional-profile-researcher.md is under agents/personal/" {
    [ -f "${PERSONAL_DIR}/professional-profile-researcher.md" ]
    [ ! -f "${AGENTS_DIR}/professional-profile-researcher.md" ]
}

# --- Plugin-scope agents stay directly under agents/ -----------------------

@test "backend-typescript-architect.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/backend-typescript-architect.md" ]
    [ ! -f "${PERSONAL_DIR}/backend-typescript-architect.md" ]
}

@test "python-backend-engineer.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/python-backend-engineer.md" ]
}

@test "ui-engineer.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/ui-engineer.md" ]
}

@test "bash-zsh-macos-engineer.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/bash-zsh-macos-engineer.md" ]
}

@test "podman-container-architect.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/podman-container-architect.md" ]
}

@test "qa-engineer.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/qa-engineer.md" ]
}

@test "senior-code-reviewer.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/senior-code-reviewer.md" ]
}

@test "meta-agent.md stays at agents/ root" {
    [ -f "${AGENTS_DIR}/meta-agent.md" ]
}

# --- Registry validator resolves references in either location -------------

@test "validator still resolves all references in the real repo" {
    run "${VALIDATOR}" "${REPO_ROOT}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

@test "validator resolves a subagent_type reference that lives under agents/personal/" {
    # Build a temporary fixture: an agent under agents/personal/ and a skill
    # that references it via subagent_type. The validator must walk the
    # subdirectory and treat the personal agent as a valid target.
    tmpdir="$(mktemp -d)"
    mkdir -p "${tmpdir}/agents/personal" "${tmpdir}/skills"
    cat >"${tmpdir}/agents/personal/personal-agent.md" <<'EOF'
---
name: personal-agent
---
fixture agent
EOF
    cat >"${tmpdir}/skills/personal-ref.md" <<'EOF'
---
name: personal-ref
---
Dispatch with subagent_type="personal-agent".
EOF
    run "${VALIDATOR}" "${tmpdir}"
    rm -rf "${tmpdir}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

# --- README documents the split --------------------------------------------

@test "README has an SDLC plugin agents heading" {
    grep -q "SDLC plugin agents" "${REPO_ROOT}/README.md"
}

@test "README has a Personal extras heading" {
    grep -q "Personal extras" "${REPO_ROOT}/README.md"
}

# --- Agent counts are exactly right ----------------------------------------

@test "agents/ root contains exactly 8 plugin agents (no extras)" {
    count="$(find "${AGENTS_DIR}" -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
    [ "${count}" -eq 8 ]
}

@test "agents/personal/ contains exactly 4 personal agents" {
    count="$(find "${PERSONAL_DIR}" -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')"
    [ "${count}" -eq 4 ]
}

# --- README lists every agent in the correct section -----------------------

@test "README plugin section lists all 8 plugin agents" {
    for agent in backend-typescript-architect python-backend-engineer ui-engineer \
                 bash-zsh-macos-engineer podman-container-architect qa-engineer \
                 senior-code-reviewer meta-agent; do
        grep -q "${agent}" "${REPO_ROOT}/README.md"
    done
}

@test "README personal section lists all 4 personal agents" {
    for agent in crypto-coin-analyzer crypto-market-agent \
                 executive-summary-generator professional-profile-researcher; do
        grep -q "${agent}" "${REPO_ROOT}/README.md"
    done
}

@test "no agent is listed under both plugin section and personal section" {
    # Extract the content between the two headings and check no personal agent name
    # appears in the plugin section.
    plugin_section="$(awk '/#### SDLC plugin agents/{p=1} /#### Personal extras/{p=0} p' "${REPO_ROOT}/README.md")"
    personal_section="$(awk '/#### Personal extras/{p=1} /^---/{p=0} p' "${REPO_ROOT}/README.md")"
    for agent in crypto-coin-analyzer crypto-market-agent \
                 executive-summary-generator professional-profile-researcher; do
        if echo "${plugin_section}" | grep -q "${agent}"; then
            echo "FAIL: personal agent '${agent}' found in plugin section" >&2
            return 1
        fi
    done
    for agent in backend-typescript-architect python-backend-engineer ui-engineer \
                 bash-zsh-macos-engineer podman-container-architect qa-engineer \
                 senior-code-reviewer meta-agent; do
        if echo "${personal_section}" | grep -q "${agent}"; then
            echo "FAIL: plugin agent '${agent}' found in personal section" >&2
            return 1
        fi
    done
}

# --- Symlink-style resolution: agents/personal/ is discoverable via agents/ -

@test "recursive walk via agents/ symlink exposes personal agents to validator" {
    # Simulate the install.sh symlink scenario: agents/ is symlinked as a whole,
    # so agents/personal/ is reachable. Validator must resolve personal names.
    tmpdir="$(mktemp -d)"
    mkdir -p "${tmpdir}/agents/personal" "${tmpdir}/skills"
    printf -- '---\nname: personal-helper\n---\n' >"${tmpdir}/agents/personal/personal-helper.md"
    printf -- '---\nname: ref-skill\n---\nsubagent_type="personal-helper"\n' >"${tmpdir}/skills/ref-skill.md"
    run "${VALIDATOR}" "${tmpdir}"
    rm -rf "${tmpdir}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

@test "nonexistent personal-agent reference fails even when agents/personal/ exists" {
    # Validates no false-positive: having agents/personal/ does NOT suppress failures
    # for genuinely missing agent names.
    tmpdir="$(mktemp -d)"
    mkdir -p "${tmpdir}/agents/personal" "${tmpdir}/skills"
    printf -- '---\nname: real-agent\n---\n' >"${tmpdir}/agents/personal/real-agent.md"
    printf -- '---\nname: bad-skill\n---\nsubagent_type="ghost-agent"\n' >"${tmpdir}/skills/bad-skill.md"
    run "${VALIDATOR}" "${tmpdir}"
    rm -rf "${tmpdir}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"ghost-agent"* ]]
}
