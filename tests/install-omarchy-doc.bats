#!/usr/bin/env bats
# Cross-reference assertions for docs/install-omarchy.md (Arch Linux / Omarchy).
#
# Mirrors install-windows-doc.bats: the guide must exist, the README must link
# to it, and it must carry the "Tested with" footer. One extra check pins the
# exact pacman invocation the guide documents to the one the installer emits.

REPO_ROOT="${BATS_TEST_DIRNAME}/.."

@test "docs/install-omarchy.md exists and is non-empty" {
    local doc="$REPO_ROOT/docs/install-omarchy.md"
    [ -f "$doc" ]
    [ -s "$doc" ]
}

@test "README.md links to docs/install-omarchy.md" {
    grep -qF "docs/install-omarchy.md" "$REPO_ROOT/README.md"
}

@test "docs/install-omarchy.md contains a Tested-with footer" {
    grep -qi "Tested with" "$REPO_ROOT/docs/install-omarchy.md"
}

@test "docs/install-omarchy.md documents the exact pacman package list the installer uses" {
    local pkgs
    pkgs="$(sed -n '/^install_tools_pacman()/,/^}/p' "$REPO_ROOT/install/tools.sh" \
        | grep -E '^\s+[a-z0-9.-]+\s+#' | awk '{print $1}' | tr '\n' ' ' | sed 's/ $//')"
    [ -n "$pkgs" ]
    grep -qF "sudo pacman -S --needed --noconfirm $pkgs" "$REPO_ROOT/docs/install-omarchy.md"
}
