#!/usr/bin/env bats
# ABOUTME: Tests for Story 27.1-002 — repo code agents must declare
# `model: sonnet` explicitly in their frontmatter so interactive Agent-tool
# dispatches stop silently inheriting the (Opus) session default. The
# orchestrator can still escalate by passing an explicit `model` at dispatch;
# that path must be documented in WORKFLOW-v2.md.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"

# Code agents in scope for the explicit-model requirement. Personal/research
# agents (agents/personal/) and meta-agent are intentionally out of scope.
CODE_AGENTS=(
  backend-typescript-architect
  bash-zsh-macos-engineer
  podman-container-architect
  python-backend-engineer
  qa-engineer
  senior-code-reviewer
  ui-engineer
)

# Extract the YAML frontmatter block (between the first two `---` lines).
frontmatter() {
  awk '/^---$/{n++; next} n==1{print} n>1{exit}' "$1"
}

@test "every code agent declares model: sonnet in frontmatter" {
  local missing=()
  for agent in "${CODE_AGENTS[@]}"; do
    local file="${REPO_ROOT}/agents/${agent}.md"
    [ -f "$file" ] || { missing+=("${agent} (file not found)"); continue; }
    if ! frontmatter "$file" | grep -qE '^model: sonnet$'; then
      missing+=("$agent")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "agents missing 'model: sonnet' frontmatter: ${missing[*]}" >&2
    return 1
  fi
}

@test "model declaration sits inside frontmatter, not the agent body" {
  for agent in "${CODE_AGENTS[@]}"; do
    local file="${REPO_ROOT}/agents/${agent}.md"
    # The literal line must not appear after the closing `---` delimiter.
    run awk '/^---$/{n++; next} n>=2 && /^model: sonnet$/{exit 1}' "$file"
    [ "$status" -eq 0 ]
  done
}

@test "agents declare model exactly once" {
  for agent in "${CODE_AGENTS[@]}"; do
    local file="${REPO_ROOT}/agents/${agent}.md"
    local count
    count="$(frontmatter "$file" | grep -cE '^model:' || true)"
    if [ "$count" -ne 1 ]; then
      echo "${agent}: expected exactly one 'model:' key, found ${count}" >&2
      return 1
    fi
  done
}

@test "WORKFLOW-v2.md documents the dispatch-time model escalation path" {
  grep -qi 'model: sonnet' "${REPO_ROOT}/WORKFLOW-v2.md"
  grep -qiE 'escalat' "${REPO_ROOT}/WORKFLOW-v2.md"
}
