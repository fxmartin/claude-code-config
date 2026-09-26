#!/usr/bin/env bats
# ABOUTME: Tests for issue #697 — /project-init supports GitLab-master repos.
# ABOUTME: Asserts the generated .sdlc-forge.yaml and credential-helper lines render byte-exact.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
PLUGIN="${REPO_ROOT}/plugins/autonomous-sdlc"
SKILL_DIR="${PLUGIN}/skills/project-init"
RULES="${SKILL_DIR}/generation-rules.md"
QUESTIONS="${SKILL_DIR}/interactive-questions.md"
SKILL="${SKILL_DIR}/SKILL.md"

# Print the body of the fenced block that follows the given heading line.
fenced_after() {
  awk -v h="$1" '
    $0 == h { seen = 1; next }
    seen && /^```/ { if (inblk) exit; inblk = 1; next }
    seen && inblk { print }
  ' "$RULES"
}

@test "forge file block renders byte-exact" {
  run fenced_after "#### .sdlc-forge.yaml (GitLab)"
  [ "$status" -eq 0 ]
  expected='# Declares the code host for the sdlc controller: origin is the local-ci-cd
# GitLab on home-lab, whose hostname carries no "gitlab" tell for auto-detection
# to key on.
forge: gitlab
gitlab_url: http://gitlab.test'
  [ "$output" = "$expected" ]
}

@test "credential helper lines render byte-exact" {
  run fenced_after "#### Credential helper (GitLab)"
  [ "$status" -eq 0 ]
  expected="git config --local credential.http://gitlab.test.helper '!glab auth git-credential'"
  [ "$output" = "$expected" ]
}

@test "origin is created on gitlab.test, never home-lab:8080" {
  grep -Fq 'git remote add origin http://gitlab.test/root/<project-name>.git' "$RULES"
  ! grep -Fq 'home-lab:8080' "$RULES"
}

@test "github remote is mirror target only and never pushed" {
  grep -Fq -- '--remote=github' "$RULES"
  grep -Fq 'git push -u origin main' "$RULES"
  ! grep -Eq 'git push[^\n]*github' "$RULES"
}

@test "labels are applied with glab label create" {
  grep -Fq 'glab label create' "$RULES"
}

@test "mirror command and split-brain warning are in the summary rules" {
  grep -Fq 'local-ci-cd mirror add root/<project-name> --username x-access-token --token-env LOCAL_CI_CD_MIRROR_GITHUB_TOKEN' "$RULES"
  grep -Fq 'keep_divergent_refs' "$RULES"
}

@test "pre-flight checks glab auth and gitlab.test reachability" {
  grep -Fq 'glab auth status' "$SKILL"
  grep -Fq 'curl -sI http://gitlab.test/' "$SKILL"
}

@test "master repo question defaults to GitLab, GitHub keeps today's flow" {
  grep -Fq 'Master repo' "$QUESTIONS"
  grep -Fq 'GitLab on home-lab (default)' "$QUESTIONS"
}

@test "generated CLAUDE.md for GitLab documents flow, no vendor, warm-up" {
  grep -Fq 'never commit `vendor/`' "$RULES"
  grep -Fq 'local-ci-cd cache warm-deps' "$RULES"
  grep -Fq 'GOMODCACHE' "$RULES"
}

@test "gitlab CI template installed and github ci kept" {
  grep -Fq 'templates/gitlab-ci.yml' "$RULES"
  grep -Fq '.github/workflows/ci.yml' "$RULES"
}

@test "eval fixtures exist" {
  [ -s "${PLUGIN}/evals/project-init-gitlab/prompt.md" ]
  [ -s "${PLUGIN}/evals/project-init-gitlab/graders/criteria.md" ]
}
