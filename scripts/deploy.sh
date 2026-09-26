#!/usr/bin/env bash
# ABOUTME: One-command deploy — installs the sdlc controller CLI and moves the
# ABOUTME: autonomous-sdlc plugin pointer to the same version, so they can't drift.
#
# deploy.sh — deploy this repo's two installable artifacts together.
#
# The controller CLI and the Claude Code plugin ship from the same repo and the
# same version number, but they install through completely separate mechanisms:
#
#   plugin     → `claude plugin update <plugin>@<marketplace>`
#   controller → `uv tool install --force controller/`   (scripts/install-controller.sh)
#
# Before either, when podman or docker is on PATH, it builds the agent sandbox
# image (controller/sandbox/Containerfile) for this host's architecture and pins
# its image id in controller/src/sdlc/config/sandbox-image.yaml — the only image
# SDLC_SANDBOX=1 will run (issue #614). Commit the updated pin. With no runtime
# the step is skipped, and SDLC_SANDBOX=1 refuses to run on this host.
#
# Running only one leaves the other on whatever version it was last explicitly
# updated to — a controller driving skills it no longer matches, or vice versa.
# That drift is silent: `git pull` moves neither pointer. This script runs both,
# plugin FIRST: it is the remote, fallible step (marketplace, network) and its
# effect is deferred until Claude Code restarts, while the controller install is
# local and idempotent. If the plugin update fails, nothing has moved; if the
# controller install then fails, the running system is still consistent (the new
# plugin loads only on restart) and re-running this script converges.
#
# Usage:
#   ./scripts/deploy.sh                  # plugin + controller
#   ./scripts/deploy.sh --controller-only  # no Claude Code on this box
#   ./scripts/deploy.sh --plugin-only
#   ./scripts/deploy.sh --skip-sandbox-image  # do not (re)build the sandbox image
#   ./scripts/deploy.sh --dry-run        # print what would run, change nothing
#   ./scripts/deploy.sh --help
#
# A default run REQUIRES `claude` on PATH. It is checked in preflight, before
# anything is installed, so a box without Claude Code aborts with the machine
# untouched rather than half-deployed: moving only one of the two pointers is the
# drift this script exists to prevent. Use --controller-only to deploy the
# controller alone, deliberately.
#
# The plugin step needs a restart of Claude Code to take effect; the controller
# step takes effect immediately.
#
# Verify afterwards with:
#   sdlc --version && claude plugin list

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The plugin this repo publishes, as `plugin@marketplace`. Both halves are
# declared in .claude-plugin/marketplace.json.
PLUGIN_ID="autonomous-sdlc@fx-claude-config"

# Test seam: tests/deploy.bats points this at a stub so the suite never runs a
# real `uv tool install`. Defaults to the real installer.
INSTALL_CONTROLLER="${INSTALL_CONTROLLER:-${SCRIPT_DIR}/install-controller.sh}"

# Sandbox image (issue #614). SANDBOX_RUNTIME forces the container runtime (and
# is the tests' stub seam); unset auto-detects podman, then docker. The pin file
# is controller package data, so it must be written BEFORE the controller
# install for the installed CLI to carry it.
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SANDBOX_CONTAINERFILE="${SANDBOX_CONTAINERFILE:-${REPO_ROOT}/controller/sandbox/Containerfile}"
SANDBOX_PIN_FILE="${SANDBOX_PIN_FILE:-${REPO_ROOT}/controller/src/sdlc/config/sandbox-image.yaml}"
SANDBOX_TAG="sdlc-agent-sandbox:local"

DO_CONTROLLER=true
DO_PLUGIN=true
DO_SANDBOX=true
DRY_RUN=false

usage() {
  sed -n '6,46p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)         usage; exit 0 ;;
    --dry-run)         DRY_RUN=true ;;
    --controller-only) DO_PLUGIN=false ;;
    --plugin-only)     DO_CONTROLLER=false ;;
    --skip-sandbox-image) DO_SANDBOX=false ;;
    *)                 die "unknown flag: $1 (try --help)" ;;
  esac
  shift
done

if [[ "${DO_CONTROLLER}" == false && "${DO_PLUGIN}" == false ]]; then
  die "--controller-only and --plugin-only are mutually exclusive"
fi

# The sandbox image belongs to the controller (its pin is controller package
# data), so --plugin-only never builds it.
[[ "${DO_CONTROLLER}" == true ]] || DO_SANDBOX=false

# OCI architecture name for this host; each host builds and pins its own.
sandbox_arch() {
  case "$(uname -m)" in
    x86_64|amd64)  echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *)             return 1 ;;
  esac
}

# The runtime to build with: $SANDBOX_RUNTIME, else podman, else docker, else "".
sandbox_runtime() {
  if [[ -n "${SANDBOX_RUNTIME:-}" ]]; then
    echo "${SANDBOX_RUNTIME}"
  elif command -v podman >/dev/null 2>&1; then
    echo podman
  elif command -v docker >/dev/null 2>&1; then
    echo docker
  fi
}

# Record `<arch>: sha256:<id>` in the pin file, replacing any existing line for
# that arch. Written via a temp file + mv (portable: no GNU/BSD `sed -i` split).
write_sandbox_pin() {
  local arch="$1" id="$2" tmp
  tmp="$(mktemp "${SANDBOX_PIN_FILE}.XXXXXX")"
  awk -v arch="${arch}" -v id="${id}" '
    $0 ~ "^" arch ":" { print arch ": " id; done = 1; next }
    { print }
    END { if (!done) print arch ": " id }
  ' "${SANDBOX_PIN_FILE}" >"${tmp}"
  mv "${tmp}" "${SANDBOX_PIN_FILE}"
}

# Preflight — validate every precondition BEFORE mutating anything.
#
# The two steps move independent pointers, and step 1 cannot be rolled back once
# `uv tool install --force` has recreated the venv. So a step-2 precondition that
# fails after step 1 succeeded leaves the controller newer than the plugin: the
# drift this script exists to prevent, merely with a non-zero exit describing it.
# Both preconditions are knowable up front, so we check them up front and abort
# with the machine untouched.
#
# Skipped under --dry-run, which mutates nothing and so cannot drift.
if [[ "${DRY_RUN}" == false ]]; then
  if [[ "${DO_CONTROLLER}" == true && ! -x "${INSTALL_CONTROLLER}" ]]; then
    die "controller installer not found or not executable: ${INSTALL_CONTROLLER}"
  fi
  if [[ "${DO_SANDBOX}" == true && -n "${SANDBOX_RUNTIME:-}" ]] \
      && ! command -v "${SANDBOX_RUNTIME}" >/dev/null 2>&1; then
    die "container runtime not found: ${SANDBOX_RUNTIME} (\$SANDBOX_RUNTIME)"
  fi
  if [[ "${DO_SANDBOX}" == true && -n "$(sandbox_runtime)" ]] && ! sandbox_arch >/dev/null; then
    die "unsupported architecture for the sandbox image: $(uname -m)
       Pass --skip-sandbox-image to deploy without it."
  fi
  if [[ "${DO_PLUGIN}" == true ]] && ! command -v claude >/dev/null 2>&1; then
    die "claude not found on PATH; cannot update ${PLUGIN_ID}.
       Nothing was changed. Install Claude Code and re-run, or pass
       --controller-only to deploy the controller alone and accept that the
       plugin stays on its current version."
  fi
fi

# 0. Sandbox image (issue #614). Built before either pointer moves: a failed
#    build (base-image pull, npm) leaves the machine untouched. The pin is the
#    image id — content-addressed, never a tag — so dispatch runs exactly this
#    build and nothing a registry could later swap under the same name.
if [[ "${DO_SANDBOX}" == true ]]; then
  RUNTIME="$(sandbox_runtime)"
  if [[ -z "${RUNTIME}" ]]; then
    log "no podman/docker on PATH; sandbox image not built (SDLC_SANDBOX=1 will refuse on this host)"
  elif [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: ${RUNTIME} build -t ${SANDBOX_TAG} -f ${SANDBOX_CONTAINERFILE}"
    log "[dry-run] would pin its image id for $(sandbox_arch) in ${SANDBOX_PIN_FILE}"
  else
    ARCH="$(sandbox_arch)"
    log "building sandbox image for ${ARCH} with ${RUNTIME}"
    "${RUNTIME}" build -t "${SANDBOX_TAG}" -f "${SANDBOX_CONTAINERFILE}" \
      "$(dirname "${SANDBOX_CONTAINERFILE}")" \
      || die "sandbox image build failed; nothing else was changed"
    IMAGE_ID="$("${RUNTIME}" image inspect --format '{{.Id}}' "${SANDBOX_TAG}")" \
      || die "could not read the sandbox image id"
    # podman prints the bare hex id, docker prefixes it with sha256:.
    IMAGE_ID="sha256:${IMAGE_ID#sha256:}"
    [[ "${IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || die "unexpected sandbox image id: ${IMAGE_ID}"
    write_sandbox_pin "${ARCH}" "${IMAGE_ID}"
    log "pinned ${ARCH} sandbox image ${IMAGE_ID} — commit ${SANDBOX_PIN_FILE#"${REPO_ROOT}/"}"
  fi
fi

# 1. Plugin pointer — the fallible step goes first (see header). `claude` was
#    proven present in preflight, but the update itself can still fail at
#    runtime (marketplace, network); failing here leaves the machine untouched.
if [[ "${DO_PLUGIN}" == true ]]; then
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: claude plugin update ${PLUGIN_ID}"
  else
    log "updating plugin ${PLUGIN_ID}"
    claude plugin update "${PLUGIN_ID}"
    log "plugin updated — restart Claude Code to load it"
  fi
fi

# 2. Controller CLI. install-controller.sh bootstraps uv when absent and is
#    itself idempotent, so re-running deploy.sh is safe. If this local step
#    fails after the plugin moved, the running system is still consistent (the
#    new plugin loads only on restart) — but say so explicitly, so nobody
#    restarts Claude Code onto a mismatched pair.
if [[ "${DO_CONTROLLER}" == true ]]; then
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: ${INSTALL_CONTROLLER}"
  else
    log "installing the sdlc controller CLI"
    if ! "${INSTALL_CONTROLLER}"; then
      if [[ "${DO_PLUGIN}" == true ]]; then
        die "controller install failed AFTER the plugin was updated.
       Do not restart Claude Code yet — the new plugin would load against the
       old controller. re-run ${BASH_SOURCE[0]} (idempotent) to converge, or
       ${BASH_SOURCE[0]} --controller-only to retry just the failed step."
      fi
      die "controller install failed: ${INSTALL_CONTROLLER}"
    fi
  fi
fi

if [[ "${DRY_RUN}" == true ]]; then
  log "dry run complete; nothing was changed"
else
  log "done. Verify with: sdlc --version && claude plugin list"
fi
