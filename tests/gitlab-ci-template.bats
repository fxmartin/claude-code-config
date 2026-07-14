#!/usr/bin/env bats
# ABOUTME: Tests for scripts/validate-gitlab-ci.sh and the shipped GitLab CI gate template.
# ABOUTME: Story 23.3-001 — the .gitlab-ci.yml quality-gate template for adopted GitLab repos.
#
# Strategy: drive the validator against the real template (templates/gitlab-ci.yml,
# the "good" case) and against fixtures under tests/fixtures/gitlab-ci/ that each
# break one acceptance criterion (missing gate job, malformed YAML, a Premium-only
# keyword). Mirrors the tests/validate-agent-registry.bats pattern.

VALIDATOR="${BATS_TEST_DIRNAME}/../scripts/validate-gitlab-ci.sh"
TEMPLATE="${BATS_TEST_DIRNAME}/../templates/gitlab-ci.yml"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/gitlab-ci"

# --- Behavioral risk-gate helpers -------------------------------------------
# The risk-gate job's shell logic lives inline in the YAML `script:` block
# rather than a standalone file (unlike scripts/risk-gate-detect.sh, which
# tests/risk-gate.bats exercises directly). These helpers extract that block
# via the same uv+PyYAML approach scripts/validate-gitlab-ci.sh uses, and build
# an isolated git fixture so the extracted script can be run for real and its
# exit-code/labelling behavior asserted rather than grepped for.

# Extracts the named job's `script:` block from ${TEMPLATE} into $1.
_extract_job_script() {
    local job="$1" out="$2"
    local src
    src=$(cat <<'PY'
import sys, yaml
path, job = sys.argv[1], sys.argv[2]
with open(path) as fh:
    doc = yaml.safe_load(fh)
script = doc[job]["script"]
print("\n".join(script) if isinstance(script, list) else script)
PY
)
    if command -v uv >/dev/null 2>&1; then
        uv run --no-project --with pyyaml --quiet python3 -c "${src}" "${TEMPLATE}" "${job}" > "${out}"
    else
        python3 -c "${src}" "${TEMPLATE}" "${job}" > "${out}"
    fi
}

# Dumps ${TEMPLATE} to $2 with top-level job $1 removed, for validator
# negative-path tests (round-trips through the same YAML parser the validator
# itself uses, so the fixture is guaranteed structurally valid otherwise).
_template_without_job() {
    local job="$1" out="$2"
    local src
    src=$(cat <<'PY'
import sys, yaml
path, job, out = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    doc = yaml.safe_load(fh)
doc.pop(job, None)
with open(out, "w") as fh:
    yaml.safe_dump(doc, fh, sort_keys=False)
PY
)
    if command -v uv >/dev/null 2>&1; then
        uv run --no-project --with pyyaml --quiet python3 -c "${src}" "${TEMPLATE}" "${job}" "${out}"
    else
        python3 -c "${src}" "${TEMPLATE}" "${job}" "${out}"
    fi
}

# Builds an isolated git repo carrying scripts/risk-gate-detect.sh and its
# config at the relative paths the job script expects, with a base commit and
# a second commit that touches $1. Echoes "<repo_path> <base_sha>".
_risk_gate_repo() {
    local changed_path="$1"
    local repo
    repo="$(mktemp -d)"
    mkdir -p "${repo}/scripts" "${repo}/controller/src/sdlc/config"
    cp "${BATS_TEST_DIRNAME}/../scripts/risk-gate-detect.sh" "${repo}/scripts/"
    cp "${BATS_TEST_DIRNAME}/../controller/src/sdlc/config/high-risk-patterns.yaml" \
        "${repo}/controller/src/sdlc/config/"
    (
        cd "${repo}" || exit 1
        git init -q
        git config core.hooksPath /dev/null
        git config user.email test@test.local
        git config user.name test
        echo readme > README.md
        git add -A
        git commit -qm base
    )
    local base_sha
    base_sha="$(cd "${repo}" && git rev-parse HEAD)"
    (
        cd "${repo}" || exit 1
        mkdir -p "$(dirname "${changed_path}")"
        echo change > "${changed_path}"
        git add -A
        git commit -qm change
    )
    echo "${repo} ${base_sha}"
}

# Stubs `curl` on PATH at $1/curl (no network calls); each invocation's args
# are appended to $2 for assertion.
_stub_curl() {
    local bindir="$1" logfile="$2"
    mkdir -p "${bindir}"
    cat > "${bindir}/curl" <<CURL_STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "${logfile}"
exit 0
CURL_STUB
    chmod +x "${bindir}/curl"
}


@test "validator is executable" {
    [ -x "${VALIDATOR}" ]
}

@test "shipped template exists at templates/gitlab-ci.yml" {
    [ -f "${TEMPLATE}" ]
}

@test "validator passes on the shipped template (default path)" {
    run "${VALIDATOR}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

@test "validator passes when the template path is given explicitly" {
    run "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "shipped template is valid YAML" {
    run "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"not valid YAML"* ]]
}

@test "template declares every required quality gate job" {
    # AC: lint (shellcheck/ruff), tests (pytest/bats), schema/contract checks,
    # secret scan, commit-format, and the high-risk approval gate — all present
    # as GitLab CI jobs.
    for job in secrets-scan shellcheck ruff json-schema commit-format risk-gate pytest bats; do
        run grep -E "^${job}:" "${TEMPLATE}"
        [ "${status}" -eq 0 ]
    done
}

@test "template declares pipeline stages" {
    run grep -E "^stages:" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "commit-format job is scoped to merge-request pipelines" {
    # commitlint must only lint MR commits, not the protected default branch
    # history (mirrors the GitHub `if: pull_request` guard).
    run grep -n "CI_MERGE_REQUEST_DIFF_BASE_SHA" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "risk-gate job is scoped to merge-request pipelines" {
    # The high-risk gate must only run on MR pipelines (it diffs against the MR
    # base sha); it must not run on protected default-branch pushes.
    run bash -c "awk '/^risk-gate:/{f=1} f&&/^[a-z]/&&!/^risk-gate:/{f=0} f' '${TEMPLATE}' | grep -F 'merge_request_event'"
    [ "${status}" -eq 0 ]
}

@test "risk-gate job invokes the shared risk-gate detector" {
    run grep -F "scripts/risk-gate-detect.sh" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "risk-gate job reads CI_MERGE_REQUEST_LABELS for the risk-approved signal" {
    run grep -F "CI_MERGE_REQUEST_LABELS" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    run grep -F "risk-approved" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "risk-gate job uses the GitLab CI token priority with a no-token fallback" {
    # Token priority GITLAB_TOKEN → GL_TOKEN → CI_JOB_TOKEN (issue-host-adapters)
    # and a graceful degrade when no token is present.
    for var in GITLAB_TOKEN GL_TOKEN CI_JOB_TOKEN; do
        run grep -F "${var}" "${TEMPLATE}"
        [ "${status}" -eq 0 ]
    done
    run grep -iE "no token|without a token|best.effort" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "risk-gate job sets GIT_DEPTH so the MR diff base is fetched" {
    # A shallow clone may omit the MR base sha the job diffs against.
    run bash -c "awk '/^risk-gate:/{f=1} f&&/^[a-z]/&&!/^risk-gate:/{f=0} f' '${TEMPLATE}' | grep -F 'GIT_DEPTH'"
    [ "${status}" -eq 0 ]
}

@test "validator fails and names risk-gate when that job is absent" {
    local stripped="${BATS_TEST_TMPDIR}/no-risk-gate.gitlab-ci.yml"
    _template_without_job risk-gate "${stripped}"
    run "${VALIDATOR}" "${stripped}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"risk-gate"* ]]
}

@test "premium fixture fails on the premium keyword, not a missing risk-gate job" {
    # tests/fixtures/gitlab-ci/premium.gitlab-ci.yml carries a risk-gate job;
    # the validator must reject it for merge_train, never for a missing gate.
    run "${VALIDATOR}" "${FIXTURES}/premium.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"merge_train"* ]]
    [[ "${output}" != *"missing required gate"* ]]
}

@test "risk-gate script exits 1 (unapproved) when a high-risk file is changed" {
    local repo base_sha script
    read -r repo base_sha <<< "$(_risk_gate_repo "db/migrations/0001_add.sql")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"

    run env \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 1 ]
    [[ "${output}" == *"high-risk files detected"* ]]
    [[ "${output}" == *"needs human approval"* ]]

    rm -rf "${repo}"
}

@test "risk-gate script exits 0 when the risk-approved label is present" {
    local repo base_sha script
    read -r repo base_sha <<< "$(_risk_gate_repo "db/migrations/0001_add.sql")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"

    run env \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="bug,risk-approved" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"approved via the risk-approved label"* ]]

    rm -rf "${repo}"
}

@test "risk-gate script exits 0 on a clean diff" {
    local repo base_sha script
    read -r repo base_sha <<< "$(_risk_gate_repo "docs/guide.md")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"

    run env \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"no high-risk files in this change set"* ]]

    rm -rf "${repo}"
}

@test "risk-gate script applies risk:high via add_labels when a token is present" {
    local repo base_sha script bindir logfile
    read -r repo base_sha <<< "$(_risk_gate_repo "db/migrations/0001_add.sql")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"
    bindir="${BATS_TEST_TMPDIR}/bin"
    logfile="${BATS_TEST_TMPDIR}/curl.log"
    _stub_curl "${bindir}" "${logfile}"

    run env \
        PATH="${bindir}:${PATH}" \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="" \
        GITLAB_TOKEN="test-token" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 1 ]
    [ -f "${logfile}" ]
    run cat "${logfile}"
    [[ "${output}" == *"add_labels=risk:high"* ]]
    [[ "${output}" == *"PRIVATE-TOKEN: test-token"* ]]

    rm -rf "${repo}"
}

@test "risk-gate script clears risk:high via remove_labels on a clean diff when a token is present" {
    local repo base_sha script bindir logfile
    read -r repo base_sha <<< "$(_risk_gate_repo "docs/guide.md")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"
    bindir="${BATS_TEST_TMPDIR}/bin"
    logfile="${BATS_TEST_TMPDIR}/curl.log"
    _stub_curl "${bindir}" "${logfile}"

    run env \
        PATH="${bindir}:${PATH}" \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="" \
        GITLAB_TOKEN="test-token" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 0 ]
    run cat "${logfile}"
    [[ "${output}" == *"remove_labels=risk:high"* ]]

    rm -rf "${repo}"
}

@test "risk-gate script honors GITLAB_TOKEN over GL_TOKEN and CI_JOB_TOKEN" {
    local repo base_sha script bindir logfile
    read -r repo base_sha <<< "$(_risk_gate_repo "db/migrations/0001_add.sql")"
    script="${BATS_TEST_TMPDIR}/risk-gate-script.sh"
    _extract_job_script risk-gate "${script}"
    bindir="${BATS_TEST_TMPDIR}/bin"
    logfile="${BATS_TEST_TMPDIR}/curl.log"
    _stub_curl "${bindir}" "${logfile}"

    run env \
        PATH="${bindir}:${PATH}" \
        CI_MERGE_REQUEST_DIFF_BASE_SHA="${base_sha}" \
        CI_MERGE_REQUEST_LABELS="" \
        GITLAB_TOKEN="primary-token" \
        GL_TOKEN="secondary-token" \
        CI_JOB_TOKEN="tertiary-token" \
        CI_API_V4_URL="https://example.invalid/api/v4" \
        CI_PROJECT_ID="1" \
        CI_MERGE_REQUEST_IID="1" \
        bash -c "cd '${repo}' && bash '${script}'"
    [ "${status}" -eq 1 ]
    run cat "${logfile}"
    [[ "${output}" == *"PRIVATE-TOKEN: primary-token"* ]]
    [[ "${output}" != *"secondary-token"* ]]
    [[ "${output}" != *"tertiary-token"* ]]

    rm -rf "${repo}"
}

@test "template uses no Premium/Ultimate-only constructs" {
    # Free/Core only — no merge trains.
    run grep -iE "merge_train" "${TEMPLATE}"
    [ "${status}" -ne 0 ]
}

@test "validator fails and names the missing job when a gate is absent" {
    run "${VALIDATOR}" "${FIXTURES}/missing-gate.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"ruff"* ]]
}

@test "validator fails on malformed YAML" {
    run "${VALIDATOR}" "${FIXTURES}/malformed.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not valid YAML"* ]]
}

@test "validator fails and names the absent 'stages' declaration" {
    # All gate jobs present but the top-level `stages:` key is missing — the
    # validator must reject it at the stages check, before the per-job checks.
    run "${VALIDATOR}" "${FIXTURES}/no-stages.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"stages"* ]]
}

@test "validator fails when the template is not a YAML mapping" {
    # Syntactically valid YAML whose top level is a sequence, not a mapping of
    # jobs/keywords — the parser must reject it as not valid.
    run "${VALIDATOR}" "${FIXTURES}/not-mapping.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not valid YAML"* ]]
}

@test "validator fails on a Premium-only keyword" {
    run "${VALIDATOR}" "${FIXTURES}/premium.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"merge_train"* ]]
}

@test "validator errors clearly when the target file does not exist" {
    run "${VALIDATOR}" "${FIXTURES}/does-not-exist.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not found"* ]]
}

@test "validator surfaces an environment error when no YAML parser is available" {
    # Neither uv nor a PyYAML-capable python3 — the py_run helper must report that
    # it needs one rather than silently mis-parsing the template. Stripping PATH
    # alone is not portable: CI's /usr/bin/python3 ships PyYAML (a dev Mac's may
    # not), so shadow python3 with a stub that fails `import yaml` to force the
    # else branch deterministically, while keeping /usr/bin:/bin for `uv` absence
    # and the script's other tools.
    local stubdir="${BATS_TEST_TMPDIR}/nopyyaml"
    mkdir -p "${stubdir}"
    printf '#!/bin/sh\nexit 1\n' > "${stubdir}/python3"
    chmod +x "${stubdir}/python3"
    run env PATH="${stubdir}:/usr/bin:/bin" bash "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"need uv or a python3 with PyYAML"* ]]
}
