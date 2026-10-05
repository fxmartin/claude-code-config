#!/usr/bin/env bash
# ABOUTME: Bootstraps a headless Linux box (the Hetzner dev box) as a resident fleet worker:
# ABOUTME: install --core, plugin, controller, git auth, fleet config, systemd unit (Story 35.2-008).
#
# fleet-bootstrap.sh — turn a fresh dev box into a worker for the claude-shared pool.
#
# `install.sh --core` and `scripts/deploy.sh` are platform-neutral, but deploy.sh
# only *updates* a plugin that is already installed, and neither writes the fleet
# config, the git credential helper or the worker unit. This wraps them with the
# steps a box with nothing but `claude`, `gh` and `uv` is missing. Every step is
# idempotent: re-running converges, and a run that changes nothing says so.
#
# Run it inside the dev shell — every tool lives there, not on the login PATH:
#
#   nix develop ~/.config/nix-dev-env -c scripts/fleet-bootstrap.sh \
#       --queue-url http://home-lab.<tailnet>:8790
#
# Besides claude, gh (logged in) and uv, the dev shell needs glab logged in to the
# local GitLab (`glab auth login --hostname gitlab.test`) — without it the run is
# refused, see --github-only — and the scanners doctor checks for, semgrep and
# osv-scanner: without them doctor is not CLEAN, and a story built here skips its
# SAST and dependency scans.
#
# Steps, in order:
#   1. ./install.sh --core                    symlink the config into ~/.claude
#   2. claude plugin marketplace add / install   the fx-claude-config marketplace
#                                                and the autonomous-sdlc plugin
#   3. scripts/deploy.sh                      plugin update + `sdlc` as a uv tool
#   4. gh auth setup-git (+ glab's helper)    git authenticates through gh/glab, never a prompt
#   5. ~/.sdlc-fleet.yaml                     queue_url: names the fleet queue
#   6. `sdlc doctor --exit-code`              the verdict on steps 1-5
#   7. templates/systemd/sdlc-worker.service  installed, enabled and started
#
# Doctor runs before the unit is installed: a worker that has not registered yet is
# a FAIL by design, so judging steps 1-5 after step 7 would fail every first run.
# Its status is the script's exit status (0 CLEAN, 1 WARN, 2 FAIL), reported after
# the unit is in place so a re-run can repair a worker that is down.
#
# Usage:
#   scripts/fleet-bootstrap.sh --queue-url URL [--github-only] [--skip-service] [--dry-run]
#   scripts/fleet-bootstrap.sh --help
#
# --queue-url  the fleet queue's base URL (written to ~/.sdlc-fleet.yaml). Optional
#              once that file already names one.
# --github-only  start the worker although glab is missing or not logged in to the
#              local GitLab. It can then sync GitHub repos only, and its free slots
#              still make its peers defer GitLab jobs to it: pin those with
#              --host home-lab. Without the flag such a box is refused, unchanged.
# --skip-service  do everything but the systemd unit (a box without systemd --user).
# --dry-run    print what would run; change nothing.
#
# Reaching the box: Tailscale SSH policy denies the XPS and public port 22 is
# home-IP-only, so operate it with `ssh -J home-lab dev-server` (or widen the ACL
# in nix-install). `loginctl enable-linger` must already be on, so the unit starts
# at boot.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Test seams: tests/fleet-bootstrap.bats points these at stubs so no run installs
# anything. The marketplace and plugin ids are the ones .claude-plugin declares.
INSTALL_SH="${INSTALL_SH:-${REPO_ROOT}/install.sh}"
DEPLOY_SH="${DEPLOY_SH:-${SCRIPT_DIR}/deploy.sh}"
UNIT_TEMPLATE="${UNIT_TEMPLATE:-${REPO_ROOT}/templates/systemd/sdlc-worker.service}"
MARKETPLACE="fx-claude-config"
PLUGIN="autonomous-sdlc"
# The local GitLab every GitLab-master repo uses (.sdlc-forge.yaml gitlab_url).
GITLAB_URL="${SDLC_GITLAB_URL:-http://gitlab.test}"
GITLAB_HOSTNAME="${GITLAB_URL#*://}"

UNIT_NAME="sdlc-worker.service"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
ENV_FILE="${HOME}/.config/sdlc/worker.env"
FLEET_CONFIG="${HOME}/.sdlc-fleet.yaml"

QUEUE_URL=""
DO_SERVICE=true
DRY_RUN=false
GITHUB_ONLY=false

usage() {
  sed -n '5,55p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Run a mutating command, or only say so under --dry-run.
run() {
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would run: $*"
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)       usage; exit 0 ;;
    --dry-run)       DRY_RUN=true ;;
    --github-only)   GITHUB_ONLY=true ;;
    --skip-service)  DO_SERVICE=false ;;
    --queue-url)     [[ $# -ge 2 ]] || die "--queue-url needs a URL"; QUEUE_URL="$2"; shift ;;
    --queue-url=*)   QUEUE_URL="${1#--queue-url=}" ;;
    *)               die "unknown flag: $1 (try --help)" ;;
  esac
  shift
done

case "${QUEUE_URL}" in
  ""|http://*|https://*) ;;
  *) die "--queue-url must be an http(s) URL, got: ${QUEUE_URL}" ;;
esac
QUEUE_URL="${QUEUE_URL%/}"

# uv tools (`sdlc`) land in ~/.local/bin, which a fresh dev shell may not carry.
export PATH="${HOME}/.local/bin:${PATH}"

# Preflight — everything knowable up front, checked before anything is changed.
for tool in claude gh uv git; do
  command -v "${tool}" >/dev/null 2>&1 \
    || die "${tool} not found on PATH — run this inside the dev shell:
       nix develop ~/.config/nix-dev-env -c $0 ..."
done
gh auth status >/dev/null 2>&1 || die "gh is not logged in (gh auth login); nothing was changed"
# Most ~/Work repos are private on the local GitLab. A worker that cannot sign in
# there fails every GitLab clone and backs off in its own memory, while the free
# slots it keeps reporting make its peers defer GitLab jobs to it (review of #851):
# worse than no worker. So it is not started without glab unless asked for.
GITLAB_READY=false
if command -v glab >/dev/null 2>&1 \
  && glab auth status --hostname "${GITLAB_HOSTNAME}" >/dev/null 2>&1; then
  GITLAB_READY=true
elif [[ "${DO_SERVICE}" == true && "${GITHUB_ONLY}" == false ]]; then
  die "glab is missing or not logged in to ${GITLAB_HOSTNAME}, so this worker could sync GitHub
       repos only — yet its free slots would make its peers defer GitLab jobs to it. Nothing was
       changed. Put glab in the dev shell, run: glab auth login --hostname ${GITLAB_HOSTNAME}
       and re-run — or re-run with --github-only and pin GitLab jobs with --host home-lab."
fi
[[ -x "${INSTALL_SH}" ]] || die "installer not found or not executable: ${INSTALL_SH}"
[[ -x "${DEPLOY_SH}" ]] || die "deploy script not found or not executable: ${DEPLOY_SH}"
if [[ "${DO_SERVICE}" == true ]]; then
  [[ -f "${UNIT_TEMPLATE}" ]] || die "unit template not found: ${UNIT_TEMPLATE}"
  command -v systemctl >/dev/null 2>&1 \
    || die "systemctl not found — pass --skip-service to bootstrap without the unit"
fi
if [[ -z "${QUEUE_URL}" && ! -f "${FLEET_CONFIG}" ]]; then
  die "--queue-url is required: ${FLEET_CONFIG} does not name a fleet queue yet"
fi

# 1. Config symlinks. install.sh is additive and idempotent.
log "install.sh --core"
run "${INSTALL_SH}" --core

# 2. Marketplace and plugin. deploy.sh's `claude plugin update` only moves a
#    pointer that exists, so a fresh box needs them added once. Each list is
#    captured, not piped: `grep -q` stops reading at its match, and under pipefail
#    the lister's SIGPIPE would read as "absent" and re-add what is already there.
MARKETPLACES="$(claude plugin marketplace list 2>/dev/null || true)"
if grep -q "${MARKETPLACE}" <<<"${MARKETPLACES}"; then
  log "marketplace ${MARKETPLACE} already added"
else
  log "adding marketplace ${MARKETPLACE} from ${REPO_ROOT}"
  run claude plugin marketplace add "${REPO_ROOT}"
fi
PLUGINS="$(claude plugin list 2>/dev/null || true)"
if grep -q "${PLUGIN}" <<<"${PLUGINS}"; then
  log "plugin ${PLUGIN} already installed"
else
  log "installing plugin ${PLUGIN}@${MARKETPLACE}"
  run claude plugin install "${PLUGIN}@${MARKETPLACE}"
fi

# 3. Plugin pointer + the `sdlc` controller as a uv tool.
log "scripts/deploy.sh"
run "${DEPLOY_SH}"

# 4. git authenticates through each forge CLI's credential helper; with
#    GIT_TERMINAL_PROMPT=0 (the unit sets it) a missing credential fails at once
#    instead of at a prompt. GH_PATH=gh keeps gh's helper a PATH lookup: inside
#    the dev shell gh would write its own /nix/store path, which a garbage
#    collection after a dev-shell update deletes.
log "gh auth setup-git"
run env GH_PATH=gh gh auth setup-git
# The GitLab's helper is glab's, as checked in preflight. A blank entry goes
# first, as `gh auth setup-git` writes for GitHub: git asks every helper that
# applies in config order and takes the first answer, so a catch-all helper from
# /etc/gitconfig or an older credential.helper would answer before glab.
if [[ "${GITLAB_READY}" == true ]]; then
  log "glab credential helper for ${GITLAB_URL}"
  run git config --global --replace-all "credential.${GITLAB_URL}.helper" ''
  run git config --global --add "credential.${GITLAB_URL}.helper" '!glab auth git-credential'
else
  # Only with --github-only or --skip-service: preflight refuses a box that would
  # start a worker this way unasked.
  warn "glab is missing or not logged in to ${GITLAB_HOSTNAME}: this worker can only sync GitHub repos.
         A GitLab job it claims fails its clone and backs off here while peers defer to its free
         slots — pin GitLab jobs with --host home-lab, or log in (glab auth login --hostname
         ${GITLAB_HOSTNAME}) and re-run."
fi

# 5. Which queue the worker drains. An existing file is the operator's: never
#    overwritten, but a different URL is worth saying out loud. The URL is data,
#    not a pattern: each character goes in its own bracket (`^` cannot, so it is
#    escaped) and a '.' in the hostname matches only a '.'.
QUEUE_URL_RE="$(printf '%s\n' "${QUEUE_URL}" | sed 's/[^^]/[&]/g; s/\^/\\^/g')"
if [[ -z "${QUEUE_URL}" ]]; then
  log "${FLEET_CONFIG} left as it is"
elif [[ ! -f "${FLEET_CONFIG}" ]]; then
  log "writing ${FLEET_CONFIG}"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would write: queue_url: ${QUEUE_URL}"
  else
    printf 'queue_url: %s\n' "${QUEUE_URL}" >"${FLEET_CONFIG}"
  fi
elif grep -Eq "^queue_url:[[:space:]]*['\"]?${QUEUE_URL_RE}/?['\"]?[[:space:]]*$" "${FLEET_CONFIG}"; then
  log "${FLEET_CONFIG} already names ${QUEUE_URL}"
else
  warn "${FLEET_CONFIG} exists and does not name ${QUEUE_URL}; left untouched — edit it if that is wrong"
fi

# 6. The verdict on steps 1-5. Reported, not fatal yet: the unit below is what
#    repairs a worker that is down.
DOCTOR_RC=0
if [[ "${DRY_RUN}" == true ]]; then
  log "[dry-run] would run: sdlc doctor --exit-code"
else
  log "sdlc doctor"
  sdlc doctor --exit-code || DOCTOR_RC=$?
fi

# 7. The resident worker. Nothing to substitute: the unit uses %h for the home.
if [[ "${DO_SERVICE}" == true ]]; then
  UNIT_PATH="${UNIT_DIR}/${UNIT_NAME}"
  run mkdir -p "${UNIT_DIR}" "$(dirname "${ENV_FILE}")" "${HOME}/.local/state/sdlc"

  # The fallback token file, 0600 and empty: tailscale whois is the identity on
  # Linux, so most boxes never put anything in it. Never loosened, never emptied.
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would ensure ${ENV_FILE} exists with mode 600"
  else
    [[ -e "${ENV_FILE}" ]] || install -m 600 /dev/null "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  fi

  CHANGED=false
  if [[ -f "${UNIT_PATH}" ]] && cmp -s "${UNIT_TEMPLATE}" "${UNIT_PATH}"; then
    log "${UNIT_NAME} already installed and current"
  else
    log "installing ${UNIT_PATH}"
    run install -m 644 "${UNIT_TEMPLATE}" "${UNIT_PATH}"
    CHANGED=true
  fi
  # Asked before `enable --now`: a worker that call starts already runs the new
  # unit, and restarting it would cut its start-up self-check short.
  WAS_ACTIVE=false
  if [[ "${DRY_RUN}" == false ]] && systemctl --user is-active --quiet "${UNIT_NAME}"; then
    WAS_ACTIVE=true
  fi
  if [[ "${CHANGED}" == true ]]; then
    run systemctl --user daemon-reload
  fi
  run systemctl --user enable --now "${UNIT_NAME}"
  # A changed unit does not touch the running worker by itself.
  if [[ "${CHANGED}" == true && "${WAS_ACTIVE}" == true ]]; then
    systemctl --user restart "${UNIT_NAME}"
  fi

  if command -v loginctl >/dev/null 2>&1 \
      && ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q '=yes'; then
    warn "linger is off: the unit starts at login, not at boot. Run: sudo loginctl enable-linger $(id -un)"
  fi
fi

if [[ "${DRY_RUN}" == true ]]; then
  log "dry run complete; nothing was changed"
  exit 0
fi

if [[ "${DOCTOR_RC}" -ne 0 ]]; then
  warn "sdlc doctor was not CLEAN (exit ${DOCTOR_RC}) before the unit was installed — see above; re-run this script after fixing it"
  exit "${DOCTOR_RC}"
fi
log "done. The worker registers once its agent probe completes (up to ~90 s); then:"
log "  sdlc doctor && sdlc queue workers      # expect: dev, host $(hostname -s), claude-shared"
log "  journalctl --user -u sdlc-worker -f"
