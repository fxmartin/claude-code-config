# Projects — where every repo lives and what the main ones do

Surveyed 2026-11-08 over ssh on all four hosts, then aligned. Refresh the
per-host table with `ls ~/projects ~/Work` on each host; refresh the forge
list with `GITLAB_HOST=gitlab.test glab api 'projects?per_page=100&simple=true&membership=true' | jq -r '.[].path'`.

## The rule

- `~/projects/<name>` is the human checkout root on every host.
- `~/Work/<name>` is the SDLC fleet worker's clone root on every host. The
  controller resolves a job's repo as `~/Work/<name>` and resets it on
  dispatch, so never hand-edit a worker clone. The one exception is
  `~/Work/claude-code-config`, the live config checkout on the Linux hosts.
- On the Macs the live config is `~/.config/nix-install/config/claude-code-config`
  (nix-install pins it) and `~/projects/nix-install` is a symlink to
  `~/.config/nix-install`.

## Per host

| Host | `~/projects` | `~/Work` | Live claude-code-config |
|------|--------------|----------|-------------------------|
| `omarchy-xps13` | 27 repos | config checkout only; no worker service, fleet token present | `~/Work/claude-code-config` |
| `dev-server` (user `fx`) | 8 repos | worker clones: agent-memory, private-health-backup | `~/Work/claude-code-config` |
| `macbook-pro-m3-max` | 52 entries (the former `~/dev`, merged 2026-11-08) | worker clones: agentic-coding-monitor, local-ai-router-manager, remarkable-sync, sprite-game | `~/.config/nix-install/config/claude-code-config` |
| `home-lab` | none, it is the appliance | worker clones: agentic-coding-monitor, daily-brief, private-health-backup | `~/.config/nix-install/config/claude-code-config` |

The worker service is `com.fxmartin.sdlc-worker` (launchd) on the Macs and
`sdlc-worker.service` (systemd user) on dev-server; home-lab also runs
`sdlc-queue`. local-ci-cd has no source checkout on home-lab, only its runtime
state under `~/.config/local-ci-cd`.

On `macbook-pro-m3-max`, `~/dev` is a symlink to `~/projects` kept for zoxide
history, Codex project entries and nix-install docs that still say `~/dev`.
Create nothing new under it; drop the link once those references are gone.

## Pillar repos

All on GitLab (`http://gitlab.test/root/<name>.git` as `origin`, GitHub as the
`github` push mirror) unless noted.

| Repo | Role |
|------|------|
| `local-ci-cd` | Provisions and operates the GitLab + runner appliance on `home-lab`. Master forge for every repo; defines the job-container contract in the CI Compatibility section of CLAUDE.md. |
| `claude-code-config` | This framework: CLAUDE.md, agents, skills, hooks, reference docs, machine profiles. GitHub master while the controller-driven PR flow runs there; `gitlab` remote kept in sync. |
| `nix-install` | nix-darwin configuration for `macbook-pro-m3-max` and `home-lab`. GitHub master. Pins claude-code-config as a submodule under `config/`. |
| `omarchy-install` | Arch/Omarchy setup and maintenance for `omarchy-xps13`. |
| `agent-memory` | Self-hosted persistent memory layer behind the `memory` MCP server shared by Claude Code, Codex, Claude.ai, ChatGPT, Hermes and Claude Desktop. |
| `agentic-coding-monitor` | Floating Omarchy widget showing live activity across agentic coding sessions, local and remote, Claude and Codex. |
| `local-ai-router-manager` | Model router and manager serving local inference from `macbook-pro-m3-max`. |
| `bootstrap-dev-server` | Ubuntu + Nix provisioning for `dev-server` (Hetzner). |
| `daily-brief` | Produces C.O.B., the unattended daily news digest, from `home-lab`. |
| `private-health-backup` | Local-first collection and backup of FX's health data. |
| `omarchy-monitoring` | System monitor plugin for the Omarchy 4 bar (`fxmartin.stats`). |
| `omarchy-voice-local` | Voice control for the Omarchy desktop (OMA). Fork of wombatoperator/omarchy-voice. |
| `local-tts` | User-level Kokoro and Piper speech service over a Unix socket, plus the `tts` CLI. |

## Other repos on GitLab

anki-card-creator, cellar (checked out as `music-library` on m3max), code-film,
fluidvoice (fork of altic-dev/FluidVoice), isolated-dev, llmbench,
local-image-generator, m3-max-llm-query, proton-drive-omarchy-integration,
remarkable-sync, samsung-zfold-8-ultra, sprite-game, story-generation-2.
Platform self-tests owned by local-ci-cd: buildah-smoke, executor-smoke,
offline-gate, toolbox-accept.

## GitHub-only repos (no GitLab project yet)

Mostly dormant, all on `macbook-pro-m3-max` unless noted: agents-infra (also
dev-server), archivist (also dev-server), ai-research, claude-agent
(dev-server), claude-training, forge, honcho-on-hetzner, infobasic-bench (also
dev-server), InfrastructureAsCode (submodules nix-install, bootstrap-dev-server,
openclaw-setup), local-ai-menubar-app, local-ai-setup, local-code-bench,
local-llm-tests, openclaw-deploy (also dev-server), openclaw-infra (remote
gone), openclaw-setup (dev-server), personal-assistant, portfolio-management,
remarkable_templates_creator, sentinel, temenos-context,
transact-retro-engineering. Per the Source Control rules a repo that becomes
active again moves to GitLab first.

## Third-party clones (not ours)

hyqs, rea, lig-spike/sdcpp (xps13); ai-playground, Rosetta_N, claw-code and
claude-code-source-code-leak (m3max, both upstreams disabled).

## Known leftovers after the 2026-11-08 alignment

- `macbook-pro-m3-max`: sixteen repos under `~/projects` have uncommitted
  changes, left as found. Local branches whose remote was deleted after a
  squash merge (`[gone]` in `git branch -vv`) remain in forge, music-library,
  local-ci-cd, llmbench, archivist and nix-install; they hold nothing
  unmerged and can be pruned at leisure.
- Upstream fixes: omarchy-install MR !6 merged 2026-11-08; nix-install PR #781
  and claude-code-config PR #969 (GitHub `main` level with GitLab) are open.
- `local-ci-cd/tests/stack/reconcile.test.ts` uses `/Users/fxmartin/dev/...`
  as fixture strings only; left alone.
