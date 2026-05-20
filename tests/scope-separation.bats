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
