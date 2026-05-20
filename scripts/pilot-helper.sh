#!/usr/bin/env bash
# ABOUTME: LTM-pilot environment-capture helper. Prints a paste-ready markdown
# ABOUTME: block colleagues drop into docs/pilot-kit/feedback-template.md.
#
# Story 6.3-001 — the pilot kit.
#
# Captured fields:
#   - OS + version
#   - Architecture (Apple Silicon / Intel / x86_64 / etc.)
#   - Shell + version
#   - Claude Code version (best-effort — `claude --version`)
#   - gh CLI version
#   - git version
#   - Install path used (asks interactively; PILOT_HELPER_NONINTERACTIVE=1 to skip)
#
# Output contract: stdout is a single markdown block starting with
#   ## Environment
# suitable for pasting straight into the feedback template's Environment
# section. Stderr is reserved for transient diagnostics; redirect or ignore.
#
# Env overrides (testing hooks):
#   PILOT_HELPER_NONINTERACTIVE=1
#     Skip the interactive install-path prompt. Used by bats and CI.
#   PILOT_HELPER_INSTALL_PATH=A|B
#     Pre-fill the install path without prompting. Wins over the prompt.
#
# Exit codes:
#   0 — markdown block printed.
#   1 — unrecoverable error (printed to stderr).
#   2 — usage error.

set -euo pipefail

print_usage() {
    cat <<'USAGE'
pilot-helper.sh — capture your environment for the LTM pilot feedback form.

Usage:
  bash scripts/pilot-helper.sh [--help]

Behaviour:
  Prints a markdown block to stdout. Copy the output into the "Environment"
  section of docs/pilot-kit/feedback-template.md.

  If stdin is a TTY, the script asks which install path (A or B) you used.
  Set PILOT_HELPER_NONINTERACTIVE=1 to skip the prompt (path field reads
  "(not provided)"). Set PILOT_HELPER_INSTALL_PATH=A or =B to pre-fill it.

Env:
  PILOT_HELPER_NONINTERACTIVE   Skip the install-path prompt (any value).
  PILOT_HELPER_INSTALL_PATH     "A" (marketplace) or "B" (install.sh).

Exit codes:
  0   markdown block printed.
  1   unrecoverable error (e.g. uname failed).
  2   usage error.
USAGE
}

# Best-effort `cmd --version` wrapper. Returns the first non-empty line, or a
# placeholder if the binary is missing / errors out. Never fails the script —
# the pilot environment is exactly where partial-data is normal.
safe_version() {
    local cmd="$1"
    local label="${2:-$cmd}"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        printf 'not installed'
        return 0
    fi
    local out
    if ! out=$("$cmd" --version 2>&1); then
        # shellcheck disable=SC2016
        # Backticks here are markdown formatting, not command substitution.
        printf '%s present but `--version` returned non-zero' "$label"
        return 0
    fi
    # First non-empty line.
    printf '%s' "$out" | awk 'NF{print; exit}'
}

# OS + version. macOS uses `sw_vers`; Linux/WSL2 falls back to `/etc/os-release`.
detect_os() {
    case "$(uname -s)" in
        Darwin)
            local product version
            product=$(sw_vers -productName 2>/dev/null || echo "macOS")
            version=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
            printf '%s %s' "$product" "$version"
            ;;
        Linux)
            if [ -r /etc/os-release ]; then
                # shellcheck disable=SC1091
                ( . /etc/os-release && printf '%s %s' "${PRETTY_NAME:-Linux}" "${VERSION_ID:-}" )
            else
                printf 'Linux (unknown distro)'
            fi
            # Tag WSL2 explicitly so it isn't mistaken for native Linux.
            if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
                printf ' [WSL2]'
            fi
            ;;
        *)
            uname -sr
            ;;
    esac
}

detect_arch() {
    local raw
    raw=$(uname -m 2>/dev/null || echo "unknown")
    case "$raw" in
        arm64|aarch64)
            # On macOS, arm64 == Apple Silicon. On Linux, aarch64 is generic.
            if [ "$(uname -s)" = "Darwin" ]; then
                printf 'Apple Silicon (%s)' "$raw"
            else
                printf 'ARM64 (%s)' "$raw"
            fi
            ;;
        x86_64|amd64)
            if [ "$(uname -s)" = "Darwin" ]; then
                printf 'Intel Mac (%s)' "$raw"
            else
                printf 'x86_64 (%s)' "$raw"
            fi
            ;;
        *)
            printf '%s' "$raw"
            ;;
    esac
}

detect_shell() {
    # SHELL is the login shell, not necessarily the one running this script.
    # Both are interesting — the user's daily-driver shell is what affects
    # the `--shell` install mode.
    local login_shell="${SHELL:-unknown}"
    local login_version="unknown"
    if [ "$login_shell" != "unknown" ] && [ -x "$login_shell" ]; then
        login_version=$("$login_shell" --version 2>/dev/null | awk 'NF{print; exit}' || true)
        : "${login_version:=unknown}"
    fi
    printf '%s — %s' "$login_shell" "$login_version"
}

prompt_install_path() {
    # 1. Explicit env-var wins (used by tests).
    if [ -n "${PILOT_HELPER_INSTALL_PATH:-}" ]; then
        printf '%s' "$PILOT_HELPER_INSTALL_PATH"
        return 0
    fi
    # 2. Non-interactive flag skips the prompt entirely.
    if [ -n "${PILOT_HELPER_NONINTERACTIVE:-}" ]; then
        printf '(not provided)'
        return 0
    fi
    # 3. No TTY → no prompt (e.g. piped into another command).
    if [ ! -t 0 ]; then
        printf '(not provided)'
        return 0
    fi
    # 4. Interactive prompt.
    printf 'Which install path did you use? [A=marketplace / B=install.sh / S=skip] ' >&2
    local answer
    read -r answer </dev/tty || answer=""
    case "$answer" in
        A|a) printf 'A (Claude Code plugin marketplace)' ;;
        B|b) printf 'B (local clone + ./install.sh)' ;;
        *)   printf '(not provided)' ;;
    esac
}

main() {
    if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
        print_usage
        exit 0
    fi
    if [ $# -gt 0 ]; then
        echo "pilot-helper.sh: unexpected argument: $1" >&2
        echo "Run with --help for usage." >&2
        exit 2
    fi

    local os arch shell_info claude_v gh_v git_v install_path
    os=$(detect_os)
    arch=$(detect_arch)
    shell_info=$(detect_shell)
    claude_v=$(safe_version claude "Claude Code")
    gh_v=$(safe_version gh "gh")
    git_v=$(safe_version git "git")
    install_path=$(prompt_install_path)

    cat <<MARKDOWN
## Environment

- **OS + version:** ${os}
- **Architecture:** ${arch}
- **Shell + version:** ${shell_info}
- **Claude Code version:** ${claude_v}
- **\`gh\` CLI version:** ${gh_v}
- **\`git\` version:** ${git_v}
- **Install path used:** ${install_path}

_Captured by \`scripts/pilot-helper.sh\` on $(date -u +'%Y-%m-%dT%H:%M:%SZ')._
MARKDOWN
}

main "$@"
