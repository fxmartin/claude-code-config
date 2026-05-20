#!/usr/bin/env bats
# Story 3.2-001 — cross-reference assertions for docs/install-windows.md.
#
# This is a docs-only story; there is no behaviour to instrument. These tests
# anchor the document in the build by verifying it exists, that the README
# links to it, and that it carries the mandatory "Tested with" footer.

REPO_ROOT="${BATS_TEST_DIRNAME}/.."

@test "docs/install-windows.md exists and is non-empty" {
    local doc="$REPO_ROOT/docs/install-windows.md"
    [ -f "$doc" ]
    [ -s "$doc" ]
}

@test "README.md links to docs/install-windows.md" {
    grep -qF "docs/install-windows.md" "$REPO_ROOT/README.md"
}

@test "docs/install-windows.md contains a Tested-with footer" {
    grep -qi "Tested with" "$REPO_ROOT/docs/install-windows.md"
}
