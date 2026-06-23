#!/usr/bin/env bats
# Tests for scripts/bump-controller-version.sh (Issue #46).
#
# Strategy: build minimal pyproject.toml + uv.lock fixtures that mirror the
# real controller layout (a `[project]` table with a version, plus a uv.lock
# with the lock-format `version = 1`, the sdlc-controller package, and an
# unrelated package). Run the bumper and assert it touches ONLY the controller
# version in both files — never the lock format and never other packages.

BUMPER="${BATS_TEST_DIRNAME}/../scripts/bump-controller-version.sh"

setup() {
    TMP="$(mktemp -d)"
    PYPROJECT="${TMP}/pyproject.toml"
    UVLOCK="${TMP}/uv.lock"

    cat > "${PYPROJECT}" <<'EOF'
[project]
name = "sdlc-controller"
version = "1.16.0"
description = "External controller."
requires-python = ">=3.11"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.version]
path = "src/sdlc/__init__.py"
EOF

    cat > "${UVLOCK}" <<'EOF'
version = 1
requires-python = ">=3.11"

[[package]]
name = "click"
version = "8.1.7"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "sdlc-controller"
version = "1.16.0"
source = { editable = "." }
dependencies = [
    { name = "typer" },
]

[[package]]
name = "typer"
version = "0.12.3"
source = { registry = "https://pypi.org/simple" }
EOF
}

teardown() {
    rm -rf "${TMP}"
}

@test "bumps the [project] version in pyproject.toml" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    run awk '/^\[project\]/{p=1} p&&/^version/{print; exit}' "${PYPROJECT}"
    [ "${output}" = 'version = "1.49.6"' ]
}

@test "bumps the sdlc-controller pin in uv.lock" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    # The line immediately after the sdlc-controller name must be the new version.
    run awk '/^name = "sdlc-controller"/{getline; print; exit}' "${UVLOCK}"
    [ "${output}" = 'version = "1.49.6"' ]
}

@test "strips a leading v (PEP 440 / semver convention)" {
    run "${BUMPER}" v2.0.0 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    run grep -c 'version = "2.0.0"' "${PYPROJECT}" "${UVLOCK}"
    # one occurrence in each file
    [[ "${output}" == *"${PYPROJECT}:1"* ]]
    [[ "${output}" == *"${UVLOCK}:1"* ]]
}

@test "accepts a version with no leading v" {
    run "${BUMPER}" 1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    run awk '/^\[project\]/{p=1} p&&/^version/{print; exit}' "${PYPROJECT}"
    [ "${output}" = 'version = "1.49.6"' ]
}

@test "never touches the uv.lock lock-format version = 1" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    run head -1 "${UVLOCK}"
    [ "${output}" = "version = 1" ]
}

@test "never touches unrelated package versions in uv.lock" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    # click and typer pins must be unchanged.
    run awk '/^name = "click"/{getline; print; exit}' "${UVLOCK}"
    [ "${output}" = 'version = "8.1.7"' ]
    run awk '/^name = "typer"/{getline; print; exit}' "${UVLOCK}"
    [ "${output}" = 'version = "0.12.3"' ]
}

@test "exactly one occurrence of the new version exists in each file" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -eq 0 ]
    run grep -c '"1.49.6"' "${PYPROJECT}"
    [ "${output}" -eq 1 ]
    run grep -c '"1.49.6"' "${UVLOCK}"
    [ "${output}" -eq 1 ]
}

@test "fails when the version argument is missing" {
    run "${BUMPER}" v1.49.6 "${PYPROJECT}"
    [ "${status}" -ne 0 ]
}

@test "fails when pyproject.toml is missing" {
    run "${BUMPER}" v1.49.6 "${TMP}/nope.toml" "${UVLOCK}"
    [ "${status}" -ne 0 ]
}

@test "fails when the sdlc-controller package is absent from uv.lock" {
    cat > "${UVLOCK}" <<'EOF'
version = 1

[[package]]
name = "click"
version = "8.1.7"
EOF
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -ne 0 ]
}

@test "fails when pyproject.toml has no [project] table" {
    cat > "${PYPROJECT}" <<'EOF'
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
EOF
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"no [project] version line"* ]]
}

@test "fails gracefully when pyproject.toml has CRLF line endings (known limitation)" {
    # CRLF causes [project]\r to not match the literal "[project]" guard — the
    # script exits non-zero rather than silently writing the wrong value.
    printf '[project]\r\nname = "sdlc-controller"\r\nversion = "1.16.0"\r\n' > "${PYPROJECT}"
    run "${BUMPER}" v1.49.6 "${PYPROJECT}" "${UVLOCK}"
    [ "${status}" -ne 0 ]
}
