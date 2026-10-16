#!/usr/bin/env bash
# ABOUTME: Bootstraps a Linux dev box (systemd units) or a Mac (LaunchAgents) as a resident fleet worker:
# ABOUTME: install --core, plugin, controller, git auth, fleet config, worker + dashboard agents (35.2-008/009).
#
# fleet-bootstrap.sh — turn a box into a fleet worker: on Linux (the Hetzner dev box)
# for the claude-shared pool, on a Mac for the pools you name.
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
# osv-scanner. Without them the run is refused too, and no flag lifts that: the
# coverage gate passes a missing scanner as SKIPPED, so a story built here would
# merge without its SAST and dependency scans.
#
# Steps, in order:
#   1. ./install.sh --core                    symlink the config into ~/.claude
#   2. claude plugin marketplace add / install   the fx-claude-config marketplace
#                                                and the autonomous-sdlc plugin
#   3. scripts/deploy.sh                      plugin update + `sdlc` as a uv tool
#   4. gh auth setup-git (+ glab's helper)    git authenticates through gh/glab, never a prompt
#   5. ~/.sdlc-fleet.yaml                     queue_url: names the fleet queue
#   6. `sdlc doctor --exit-code`              the verdict on steps 1-5
#   7. Linux: templates/systemd/sdlc-worker.service and sdlc-dashboard.service
#                                             installed, enabled and started
#      macOS: templates/launchd/ worker + dashboard (+ queue with --queue-service),
#             rendered for this Mac and (re)loaded — see "On a Mac" below
#
# Doctor runs before the unit is installed: a worker that has not registered yet is
# a FAIL by design, so judging steps 1-5 after step 7 would fail every first run.
# Its status is the script's exit status (0 CLEAN, 1 WARN, 2 FAIL), reported after
# the unit is in place so a re-run can repair a worker that is down.
#
# Usage:
#   scripts/fleet-bootstrap.sh --queue-url URL [--github-only] [--skip-service] [--dry-run]
#   scripts/fleet-bootstrap.sh --worker NAME --pool P [--pool P ...] [--host HOST] [--slots N] \
#       --queue-url URL [--queue-service [--queue-allow LOGIN]] [--github-only] [--skip-service] [--dry-run]
#   scripts/fleet-bootstrap.sh --help
#
# --queue-url  the fleet queue's base URL (written to ~/.sdlc-fleet.yaml). Optional
#              once that file already names one.
# --github-only  start the worker although glab is missing or not logged in to the
#              local GitLab. It can then sync GitHub repos only, and its free slots
#              still make its peers defer GitLab jobs to it: pin those with
#              --host home-lab. Without the flag such a box is refused, unchanged.
# --skip-service  do everything but the systemd unit (a box without systemd --user).
#              Starting no worker, it is not refused for a missing glab or scanner.
# --dry-run    print what would run, and on a Mac the diff of every agent it would
#              replace; change nothing.
#
# On a Mac (Darwin) --worker and --pool are required — the Linux unit is always worker
# `dev` on the claude-shared pool, so it refuses both. Each agent is rendered from
# templates/launchd/ with this account's home and `tailscale ip -4` (the worker gets
# `--dashboard-url http://<tailnet ip>:8787`, the dashboard binds that address), and
# compared with what is installed in ~/Library/LaunchAgents:
#   - byte-identical and loaded: left alone;
#   - different or absent: written, then booted out and bootstrapped again —
#     except the worker while it has a job in flight, which is reported as
#     `pending restart (job N running)` and left for the next run;
#   - without a tailnet address (`tailscale ip -4` prints nothing) the run stops
#     before anything is written: a blank host would bind every interface.
# --host, --slots  the host name the worker registers as (a job's `--host` pin matches
#              it; default `hostname -s` — the M3 Max, host m3-max, registers as m3max)
#              and its slot cap, passed to `sdlc queue run`. Omitted, each keeps the
#              installed worker agent's value, so a re-run never drops them.
# --queue-service  also install com.fxmartin.sdlc-queue (home-lab only). The Tailscale
#              login(s) allowed to call it come from --queue-allow, else from the
#              queue agent already installed.
# Linux gains the dashboard as a second user unit, sdlc-dashboard.service, bound to the
# same address (and refused the same way without one).
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

DASHBOARD_UNIT_TEMPLATE="${DASHBOARD_UNIT_TEMPLATE:-${REPO_ROOT}/templates/systemd/sdlc-dashboard.service}"
LAUNCHD_TEMPLATES="${LAUNCHD_TEMPLATES:-${REPO_ROOT}/templates/launchd}"

UNIT_NAME="sdlc-worker.service"
DASHBOARD_UNIT_NAME="sdlc-dashboard.service"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
ENV_FILE="${HOME}/.config/sdlc/worker.env"
FLEET_CONFIG="${HOME}/.sdlc-fleet.yaml"

# The LaunchAgents (macOS). Labels are the templates' file names; doctor finds them by it.
AGENT_DIR="${HOME}/Library/LaunchAgents"
WORKER_LABEL="com.fxmartin.sdlc-worker"
DASHBOARD_LABEL="com.fxmartin.sdlc-dashboard"
QUEUE_LABEL="com.fxmartin.sdlc-queue"

PLATFORM="$(uname -s)"
QUEUE_URL=""
WORKER_NAME=""
POOLS=()
WORKER_HOST=""
SLOTS=""
QUEUE_SERVICE=false
QUEUE_ALLOW=""
DO_SERVICE=true
DRY_RUN=false
GITHUB_ONLY=false

usage() {
  # The header comment from line 5 to its end: no line number to keep in step with it.
  awk 'NR < 5 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
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
    --queue-service) QUEUE_SERVICE=true ;;
    --worker)        [[ $# -ge 2 ]] || die "--worker needs a name"; WORKER_NAME="$2"; shift ;;
    --worker=*)      WORKER_NAME="${1#--worker=}" ;;
    --pool)          [[ $# -ge 2 ]] || die "--pool needs a name"; POOLS+=("$2"); shift ;;
    --pool=*)        POOLS+=("${1#--pool=}") ;;
    --host)          [[ $# -ge 2 ]] || die "--host needs a name"; WORKER_HOST="$2"; shift ;;
    --host=*)        WORKER_HOST="${1#--host=}" ;;
    --slots)         [[ $# -ge 2 ]] || die "--slots needs a number"; SLOTS="$2"; shift ;;
    --slots=*)       SLOTS="${1#--slots=}" ;;
    --queue-allow)   [[ $# -ge 2 ]] || die "--queue-allow needs a login"; QUEUE_ALLOW="$2"; shift ;;
    --queue-allow=*) QUEUE_ALLOW="${1#--queue-allow=}" ;;
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

# Names and the login are written into a plist and a sed program: keep them to the
# characters a worker name, a pool and a Tailscale login are made of.
IS_DARWIN=false
[[ "${PLATFORM}" == Darwin ]] && IS_DARWIN=true
valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]; }
# The string after <string>FLAG</string> in the installed worker agent, or nothing.
installed_worker_arg() {
  sed -n "/<string>$1<\/string>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;q;}" \
    "${AGENT_DIR}/${WORKER_LABEL}.plist" 2>/dev/null || true
}
if [[ "${IS_DARWIN}" == true ]]; then
  if [[ "${DO_SERVICE}" == true ]]; then
    [[ -n "${WORKER_NAME}" ]] || die "--worker NAME is required on macOS (the name this Mac's worker registers as)"
    [[ ${#POOLS[@]} -gt 0 ]] || die "at least one --pool P is required on macOS (the subscription pools this Mac serves)"
    # Not in the template, so a re-render would drop them: an M3 back on host m3-max
    # strands every job pinned --host m3max. Kept like the queue agent's login.
    WORKER_HOST="${WORKER_HOST:-$(installed_worker_arg --host)}"
    SLOTS="${SLOTS:-$(installed_worker_arg --slots)}"
  fi
  [[ -z "${WORKER_NAME}" ]] || valid_name "${WORKER_NAME}" || die "--worker must be letters, digits, '.', '_' or '-': ${WORKER_NAME}"
  for pool in ${POOLS[@]+"${POOLS[@]}"}; do
    valid_name "${pool}" || die "--pool must be letters, digits, '.', '_' or '-': ${pool}"
  done
  [[ -z "${WORKER_HOST}" ]] || valid_name "${WORKER_HOST}" || die "--host must be letters, digits, '.', '_' or '-': ${WORKER_HOST}"
  [[ -z "${SLOTS}" || "${SLOTS}" =~ ^[1-9][0-9]*$ ]] || die "--slots must be a whole number, at least 1: ${SLOTS}"
  [[ -z "${QUEUE_ALLOW}" || "${QUEUE_ALLOW}" =~ ^[A-Za-z0-9@._+,-]+$ ]] || die "--queue-allow is a Tailscale login (or a comma-separated list): ${QUEUE_ALLOW}"
else
  # The unit is one worker, `dev`, on claude-shared: there is nothing to name.
  { [[ -z "${WORKER_NAME}" && ${#POOLS[@]} -eq 0 && -z "${WORKER_HOST}" && -z "${SLOTS}" \
      && "${QUEUE_SERVICE}" == false && -z "${QUEUE_ALLOW}" ]]; } \
    || die "--worker, --pool, --host, --slots, --queue-service and --queue-allow are macOS options: the Linux unit is worker dev on claude-shared"
fi

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
# The coverage gate reports a missing scanner SKIPPED, and SKIPPED passes: a story
# built here would merge without the SAST and dependency scans a Mac blocks it on
# (review of #851). Unlike --github-only, there is no safe way to run such a worker,
# so no flag starts it.
if [[ "${DO_SERVICE}" == true ]]; then
  MISSING_SCANNERS=""
  for scanner in semgrep osv-scanner; do
    command -v "${scanner}" >/dev/null 2>&1 \
      || MISSING_SCANNERS="${MISSING_SCANNERS:+${MISSING_SCANNERS} and }${scanner}"
  done
  [[ -z "${MISSING_SCANNERS}" ]] || die "${MISSING_SCANNERS} not found on PATH, so a story built here would
       merge without its SAST and dependency scans: the coverage gate reports a missing scanner
       SKIPPED, which passes. Nothing was changed. Put semgrep and osv-scanner in the dev shell
       (semgrep also installs with: uv tool install semgrep) and re-run — or bootstrap without
       the worker: --skip-service."
fi
[[ -x "${INSTALL_SH}" ]] || die "installer not found or not executable: ${INSTALL_SH}"
[[ -x "${DEPLOY_SH}" ]] || die "deploy script not found or not executable: ${DEPLOY_SH}"
# The dashboard binds this box's tailnet address and nothing else. With Tailscale
# stopped or logged out `tailscale ip -4` prints nothing, and a blank host would
# bind every interface: stop here, before anything is written.
TAILNET_IP=""
if [[ "${DO_SERVICE}" == true ]]; then
  command -v tailscale >/dev/null 2>&1 \
    || die "tailscale not found — the dashboard binds this box's tailnet address; pass --skip-service to bootstrap without it"
  TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  [[ "${TAILNET_IP}" =~ ^[0-9]+(\.[0-9]+){3}$ ]] \
    || die "tailscale ip -4 printed no address (is Tailscale up?) — the dashboard would bind every
       interface. Nothing was changed; start Tailscale and re-run."
  if [[ "${IS_DARWIN}" == true ]]; then
    for tool in launchctl jq; do
      command -v "${tool}" >/dev/null 2>&1 || die "${tool} not found — needed to (re)load the LaunchAgents; pass --skip-service to bootstrap without them"
    done
    for label in "${WORKER_LABEL}" "${DASHBOARD_LABEL}" "${QUEUE_LABEL}"; do
      [[ -f "${LAUNCHD_TEMPLATES}/${label}.plist" ]] || die "agent template not found: ${LAUNCHD_TEMPLATES}/${label}.plist"
    done
    if [[ "${QUEUE_SERVICE}" == true && -z "${QUEUE_ALLOW}" ]]; then
      # An installed queue agent already names who may call it: keep that.
      QUEUE_ALLOW="$(sed -n '/<key>SDLC_QUEUE_ALLOW<\/key>/{n;s/.*<string>\(.*\)<\/string>.*/\1/p;}' \
        "${AGENT_DIR}/${QUEUE_LABEL}.plist" 2>/dev/null || true)"
      [[ -n "${QUEUE_ALLOW}" ]] \
        || die "--queue-service needs --queue-allow LOGIN (the Tailscale login(s) allowed to call the queue)"
    fi
  else
    [[ -f "${UNIT_TEMPLATE}" ]] || die "unit template not found: ${UNIT_TEMPLATE}"
    [[ -f "${DASHBOARD_UNIT_TEMPLATE}" ]] || die "unit template not found: ${DASHBOARD_UNIT_TEMPLATE}"
    command -v systemctl >/dev/null 2>&1 \
      || die "systemctl not found — pass --skip-service to bootstrap without the unit"
  fi
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
  # From the repo, not the caller's directory: doctor finds its repo from the
  # cwd's git root, and from ~ the harness-pin check WARNs on a healthy box.
  (cd "${REPO_ROOT}" && sdlc doctor --exit-code) || DOCTOR_RC=$?
fi

# 7. The resident worker and its dashboard. Rendered copies live in a scratch
#    directory, so a dry run can diff them and a real one can compare them byte
#    for byte with what is installed.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# Linux: install one unit file, enable it, and restart it only if it changed under
# a running process. Asked before `enable --now`: a unit that call starts already
# runs the new file, and restarting it would cut a worker's start-up self-check short.
install_systemd_unit() {  # NAME SOURCE — SOURCE is the exact text to install
  local name="$1" source="$2" path="${UNIT_DIR}/$1" changed=false was_active=false
  if [[ -f "${path}" ]] && cmp -s "${source}" "${path}"; then
    log "${name} already installed and current"
  else
    log "installing ${path}"
    run install -m 644 "${source}" "${path}"
    changed=true
  fi
  if [[ "${DRY_RUN}" == false ]] && systemctl --user is-active --quiet "${name}"; then
    was_active=true
  fi
  if [[ "${changed}" == true ]]; then
    run systemctl --user daemon-reload
  fi
  run systemctl --user enable --now "${name}"
  # A changed unit does not touch the running process by itself.
  if [[ "${changed}" == true && "${was_active}" == true ]]; then
    systemctl --user restart "${name}"
  fi
}

# macOS: `sed` the template the way its header documents, with this account's values.
# The values are validated above, so the only characters to guard are sed's own.
sed_escape() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
render_launch_agent() {  # LABEL — the rendered plist on stdout
  sed -e "s|__HOME__|$(sed_escape "${HOME}")|g" \
      -e "s|__USER__|$(sed_escape "$(id -un)")|g" \
      -e "s|__TAILNET_IP__|${TAILNET_IP}|g" \
      -e "s|__LOGIN__|$(sed_escape "${QUEUE_ALLOW}")|g" \
      "${LAUNCHD_TEMPLATES}/$1.plist"
}
# The worker template carries the M3 Max's name and pools; this Mac's own replace
# them — every `--pool` pair collapses into one pair per requested pool — and its
# --host / --slots, which the template has none of, follow the name.
render_worker_agent() {
  render_launch_agent "${WORKER_LABEL}" | awk -v worker="${WORKER_NAME}" -v pools="$(IFS=,; echo "${POOLS[*]}")" \
      -v host="${WORKER_HOST}" -v slots="${SLOTS}" '
    /<string>--worker<\/string>/ {
      print; if ((getline) > 0) print "    <string>" worker "</string>"
      if (host != "") { print "    <string>--host</string>"; print "    <string>" host "</string>" }
      if (slots != "") { print "    <string>--slots</string>"; print "    <string>" slots "</string>" }
      next
    }
    /<string>--pool<\/string>/ {
      getline
      if (!pooled) {
        n = split(pools, each, ",")
        for (i = 1; i <= n; i++) { print "    <string>--pool</string>"; print "    <string>" each[i] "</string>" }
        pooled = 1
      }
      next
    }
    { print }'
}

# The job ids running on this worker, comma-separated; nonzero if the queue cannot be read.
# Under the name the installed agent registered: a rename's old jobs are the ones a bootout kills.
worker_jobs_in_flight() {
  local listing worker
  worker="$(installed_worker_arg --worker)"
  listing="$(sdlc queue list --json 2>/dev/null)" || return 1
  jq -r --arg worker "${worker:-${WORKER_NAME}}" \
    '[.jobs[] | select(.state == "running" and .worker == $worker) | .id] | join(", ")' <<<"${listing}"
}

install_launch_agent() {  # LABEL RENDERED_FILE
  local label="$1" rendered="$2" dest="${AGENT_DIR}/$1.plist" loaded=false domain
  domain="gui/$(id -u)"
  launchctl print "${domain}/${label}" >/dev/null 2>&1 && loaded=true

  if [[ -f "${dest}" ]] && cmp -s "${rendered}" "${dest}"; then
    if [[ "${loaded}" == true ]]; then
      log "${label} already installed and current"
    else
      log "${label} is current but not loaded"
      run launchctl bootstrap "${domain}" "${dest}"
    fi
    return
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    local before=/dev/null verb=install
    if [[ -f "${dest}" ]]; then before="${dest}"; verb=replace; fi
    log "[dry-run] would ${verb} ${dest}:"
    diff -u "${before}" "${rendered}" || true
    return
  fi

  # Replacing the worker kills its jobs' supervisor: SIGTERM stops each in-flight job.
  # Unknown counts as busy — a lost build costs more than a run's delay.
  if [[ "${label}" == "${WORKER_LABEL}" && "${loaded}" == true ]]; then
    local running
    if running="$(worker_jobs_in_flight)"; then
      if [[ -n "${running}" ]]; then
        local noun="job ${running} running"
        [[ "${running}" == *,* ]] && noun="jobs ${running} running"
        log "${label}: pending restart (${noun}); left as installed — re-run once it is idle"
        return
      fi
    else
      log "${label}: pending restart (cannot read the queue to tell whether a job is running); left as installed"
      return
    fi
  fi

  log "installing ${dest}"
  mkdir -p "${AGENT_DIR}" "${HOME}/.local/state/sdlc"
  install -m 644 "${rendered}" "${dest}"
  if [[ "${loaded}" == true ]]; then
    launchctl bootout "${domain}/${label}"
  fi
  launchctl bootstrap "${domain}" "${dest}"
}

if [[ "${DO_SERVICE}" == true && "${IS_DARWIN}" == true ]]; then
  # The dashboard first: the worker advertises it.
  render_launch_agent "${DASHBOARD_LABEL}" >"${WORK_DIR}/dashboard.plist"
  render_worker_agent >"${WORK_DIR}/worker.plist"
  install_launch_agent "${DASHBOARD_LABEL}" "${WORK_DIR}/dashboard.plist"
  install_launch_agent "${WORKER_LABEL}" "${WORK_DIR}/worker.plist"
  if [[ "${QUEUE_SERVICE}" == true ]]; then
    render_launch_agent "${QUEUE_LABEL}" >"${WORK_DIR}/queue.plist"
    install_launch_agent "${QUEUE_LABEL}" "${WORK_DIR}/queue.plist"
  fi
elif [[ "${DO_SERVICE}" == true ]]; then
  run mkdir -p "${UNIT_DIR}" "$(dirname "${ENV_FILE}")" "${HOME}/.local/state/sdlc"

  # The fallback token file, 0600 and empty: tailscale whois is the identity on
  # Linux, so most boxes never put anything in it. Never loosened, never emptied.
  if [[ "${DRY_RUN}" == true ]]; then
    log "[dry-run] would ensure ${ENV_FILE} exists with mode 600"
  else
    [[ -e "${ENV_FILE}" ]] || install -m 600 /dev/null "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  fi

  # Nothing to substitute in the worker unit: it uses %h for the home.
  sed "s|__TAILNET_IP__|${TAILNET_IP}|g" "${DASHBOARD_UNIT_TEMPLATE}" >"${WORK_DIR}/${DASHBOARD_UNIT_NAME}"
  install_systemd_unit "${UNIT_NAME}" "${UNIT_TEMPLATE}"
  install_systemd_unit "${DASHBOARD_UNIT_NAME}" "${WORK_DIR}/${DASHBOARD_UNIT_NAME}"

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
  warn "sdlc doctor was not CLEAN (exit ${DOCTOR_RC}) before the unit/agents were installed — see above; re-run this script after fixing it"
  exit "${DOCTOR_RC}"
fi
log "done. The worker registers once its agent probe completes (up to ~90 s); then:"
if [[ "${IS_DARWIN}" == true ]]; then
  log "  sdlc doctor && sdlc queue workers      # expect: ${WORKER_NAME:-your worker}, host $(hostname -s), pools ${POOLS[*]:-}"
  log "  tail -f ~/.local/state/sdlc/worker.log"
else
  log "  sdlc doctor && sdlc queue workers      # expect: dev, host $(hostname -s), claude-shared"
  log "  journalctl --user -u sdlc-worker -f"
fi
