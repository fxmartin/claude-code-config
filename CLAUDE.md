# Claude Code — Global Development Instructions

## Core Principles
- **Simple, clean, maintainable solutions** over complex/clever implementations
- **Surgical changes** - smallest reasonable diff. Ask permission before reimplementing from scratch. Match existing style, even if you'd do it differently. If you notice unrelated dead code, mention it — don't delete it. Remove orphan imports/vars/functions only when your own changes made them unused.
- **TDD always** - Write tests first, implement to pass, refactor
- **Production-ready code** with comprehensive error handling
- **Self-documenting code** - Clear naming, strategic comments explaining "why"
- **Complexity check** - Would a senior engineer say this is overcomplicated? If yes, simplify.
- **NEVER use --no-verify** when committing

## Communication Style
- Address developer as **"FX"**
- Sharp, efficient, no-nonsense approach
- Business-minded with C-level context awareness
- Challenge when needed, push back on inefficiency
- Clear, structured responses with actionable insights
- ALWAYS ask for clarification rather than making assumptions.
- If you're having trouble with something, it's ok to stop and ask for help. Especially if it's something your human might be better at.

## Operating Rules (FX, 2026-11-08)
- **Config changes go to GitLab before and after.** Before editing anything in a
  config repo (`~/Work/claude-code-config` first of all), commit and push the
  current state to the `gitlab` remote; after the edit, commit and push again.
  Never leave a config change uncommitted or unpushed.
- **Tell FX before any command that needs root.** Say what the command is and
  why `sudo` is required, then wait for the go-ahead. Prefer the non-root
  alternative when one exists.
- **Never print passwords or tokens.** Not in output, logs, commit messages,
  memories or summaries. Redact them from command output (`.credentials.json`,
  `settings.json` env blocks, `.env`, keyring and `op` output).

## Machines

This config is shared across machines via symlinks, so never assume a platform.
Check with `uname -s` (`Darwin` / `Linux`) before reaching for a platform tool.

| Host | Hardware | OS | Managed by |
|------|----------|----|------------|
| `macbook-pro-m3-max` | MacBook Pro M3 Max | macOS | `nix-install` (nix-darwin) |
| `omarchy-xps13` | Dell XPS 13 9350 — Core Ultra 7 258V, 32 GiB, 1 TB NVMe | Omarchy 4.5.0 — Arch Linux, Hyprland 0.56 (Lua config), Foot, bash 5; profile below | `omarchy-install` |
| `home-lab` | MacBook Pro M1 Pro (`MacBookPro18,3`) | macOS | `nix-install` (nix-darwin) |

`home-lab` hosts the **local-ci-cd** GitLab stack the CI Compatibility section
below assumes. Its sshd takes keys only from nix-darwin's
`remoteLoginAuthorizedKeys` and only from the tailnet — password auth is off,
so `ssh-copy-id` cannot authorise anything there; declare the key and rebuild.

On Omarchy, prefer what the distro already ships over hand-rolling an equivalent:

- `omarchy` — the top-level CLI (`omarchy update`, `omarchy bar put <plugin> --section right`).
- The `omarchy-*` script family (e.g. `omarchy-microphone-test`,
  `omarchy-nvme-suspend-fix`). Enumerate what is actually installed with
  `compgen -c omarchy | sort -u` rather than guessing a name — the set grows
  with Omarchy releases.
- `pacman` for packages — never `brew`. `systemctl --user` for user services.
- The Omarchy menu is `Super + Space`; the terminal is Foot (`Super + Return`).

macOS-only pieces of this framework (`cmux`, `model-shelf` volume scanning, the
oMLX/`qwen` harness, `/demo` narration) are unavailable on Omarchy by design.

## omarchy-xps13 — Machine Profile

Surveyed 2026-11-08 without root. Refresh with `omarchy-debug --no-sudo --print`
(call the binary directly: on 4.5.0 the `omarchy debug` dispatcher rejects the
flags the shipped skill prescribes), `inxi -Fxxxz --no-sudo`, `hyprctl monitors`,
`hyprctl devices`, `lsusb -t`, `boltctl list`, `bluetoothctl devices Connected`,
`pacman -Qe`.

**Identity and firmware**
- Dell XPS 13 9350, board `0MMW13` rev A01, SKU `0CC9`. BIOS 1.24.1
  (2026-08-03). UEFI 2.70, Secure Boot off, TPM 2.0 (`/dev/tpm0`), measured UKI.
- Omarchy 4.5.0-3 (`omarchy-version`), kernel `linux-omarchy` 7.2.15, systemd 262,
  Hyprland 0.56.2 under uwsm, SDDM display manager, Limine 12.9.4 bootloader.
- Locale `en_US.UTF-8`, keyboard `fr` (console, X11 and Hyprland, option
  `omarchy:shift_caps_compose`), time zone Europe/Luxembourg, NTP on.
- Fingerprint reader enrolled for `fxmartin` (Goodix MOC, right index finger).

**Compute**
- CPU: Intel Core Ultra 7 258V (Lunar Lake), 4 P-cores up to 4.8 GHz + 4 E-cores
  up to 3.7 GHz, 8 threads (no SMT), VT-x. L2 14 MiB, L3 12 MiB. Governor
  `intel_pstate` powersave / `balance_power`; `power-profiles-daemon` on
  `balanced`; `thermald` and `intel_lpmd` running. Microcode 0x128; every
  listed CPU vulnerability is mitigated or not affected.
- RAM: 32 GiB LPDDR5X on the CPU package, not upgradable. Per-module detail
  needs root (`dmidecode -t 17`).
- GPU: Intel Arc 140V (Xe2, kernel driver `xe`, Mesa Vulkan 1.4), integrated
  only. Intel media driver, compute runtime, Level Zero, oneAPI MKL and DPC++
  installed. `ggml-vulkan` and `whisper-cpp` are built against it.
- NPU: Intel AI Boost present (`intel_vpu`, `/dev/accel/accel0`), idle at survey.

**Storage and boot**
- SK hynix PVC10 1024 GB NVMe (DRAM-less, PCIe 4.0 x4, firmware 61001171), 30 °C.
  SMART needs root (`smartctl`, `nvme smart-log`).
- GPT: `nvme0n1p1` 2 GiB vfat `/boot`; `nvme0n1p2` LUKS → `/dev/mapper/root`
  btrfs, subvolume `@` on `/` plus `/home`, `/var/log`, `/var/cache/pacman/pkg`.
  ~217 GiB used of 952 GiB. Snapper snapshots synced to Limine
  (`limine-snapper-sync`); `snapper list` needs root.
- Swap: 31 GiB zram (zstd, priority 100) + 31 GiB `/swap/swapfile` (priority 0)
  with `resume=` on the kernel cmdline, so hibernate to the swapfile is
  configured; hibernate image ~12 GiB. Swappiness 150, cache-pressure 50.
  Suspend is s2idle.

**Display, audio, camera, input**
- Internal panel eDP-1: LG Display 13.4" 1920x1200 @ 120 Hz (60 Hz available),
  scale 1.25 set in `~/.config/hypr/monitors.lua`, sRGB preset. Three DP
  outputs (`DP-1..3`) exist through USB-C, none connected.
- Audio: Intel HD Audio over SoundWire (`sof-audio-pci-intel-lnl`), PipeWire
  1.6.9 + WirePlumber 0.5.18. Sinks: speaker (default) and three HDMI/DP;
  source: SoundWire microphone array.
- Camera: OmniVision OV02C10 on Intel IPU7 (`intel-ipu7`, libcamera). The USB
  device "SLS Lattice AI USB 2.0" is the internal camera's USBIO bridge, not a
  peripheral.
- Input: Dell keyboard and touchpad (`VEN_0488:1072`), lid switch, Dell WMI
  hotkeys and privacy driver, Intel HID 5-button array. fcitx5's virtual
  keyboard is Hyprland's main keyboard device.

**Wireless, ports, power**
- Wi-Fi: Intel BE200 Wi-Fi 7 (`iwlwifi`, interface `wlp0s20f3`). Bluetooth 5.4
  (`btintel_pcie`, `hci0`, adapter name `omarchy-xps13`).
- Ports: two USB-C / Thunderbolt 4 (USB4) ports (`00:07.0`, `00:07.2`, Intel
  Gen16 controllers via `bolt`). No USB-A, HDMI or Ethernet on the chassis.
- Battery: Dell `J7H5M58C` 54 Wh Li-polymer, 39 cycles, health ~100 %,
  adaptive charge window 50–90 %. Two fans (max 6800 / 6500 rpm), readable via
  `dell_smm_hwmon`; `sensors` covers CPU, NVMe, Wi-Fi and chassis temperatures.

**Peripherals (state at survey)**
- Nothing external attached: on battery, both USB-C ports empty, only internal
  USB devices (camera bridge, Goodix fingerprint). All Thunderbolt DP outputs
  disconnected.
- Known dock: Ugreen TBT5 Docking Station 17-in-1 (USB4), authorised in `bolt`
  with `iommu` policy, last connected 2026-11-05.
- Bluetooth pairings include François-Xavier's AirPods, iPad mini 7, Plaud Note
  Pro, Samsung The Frame 55, several Schneider BLE devices and a Hue Signe
  gradient floor lamp. None connected at survey time.
- Network: Wi-Fi on `FRITZ!Box 7490`, 5 GHz channel 100, 80 MHz, WPA2.
  Tailscale up as `omarchy-xps13`; active peers at survey: `home-lab`,
  `macbook-pro-m3-max`, `dev-server`, `hermes-agent`, `nas-luxembourg`.
  `gitlab.test` resolves through the tailnet (split DNS).

**Desktop and where settings live**
- Hyprland is configured in Lua (Omarchy 4): `~/.config/hypr/hyprland.lua`
  bootstraps `/usr/share/omarchy/default/hypr/` and then loads the personal
  overrides `monitors.lua`, `input.lua`, `bindings.lua`, `looknfeel.lua`,
  `autostart.lua`. Edit only the `~/.config` files; the `/usr/share/omarchy`
  tree is owned by the package.
- Omarchy shell (bar, plugins, idle/lock): `~/.config/omarchy/shell.json`.
  Hooks: `~/.config/omarchy/hooks/{battery-low,font-set,post-boot,post-update,pre-refresh-pacman,theme-set}.d`.
  Plugins: `~/.config/omarchy/plugins/` (`fxmartin.acm`, `fxmartin.stats`,
  `nixfred.infomarchy`, `jankeesvw.time-machine`, `io.github.thisisgm.omapods`,
  `akshar.radio-atlas`, `voice.indicator`, `voice.orb`). Menu extension:
  `~/.config/omarchy/extensions/omarchy-menu.jsonc`. Themes: `themes/`,
  `themed/` templates, background set `flexoki-light`, branding in `branding/`.
- Shipped defaults for every app live in `/usr/share/omarchy/config/<app>`;
  user copies are in `~/.config/<app>` (foot, starship, btop, lazygit, tmux,
  nvim, wireplumber, fcitx5, xdg-desktop-portal, …).
- Terminal Foot (`~/.config/foot/foot.ini`), bash 5 + starship
  (`~/.config/starship.toml`), editor `omarchy-launch-editor` → Neovim 0.12
  (`omarchy-nvim`). Input method fcitx5 with mozc, chewing, hangul add-ons.
- The `omarchy` CLI has 40+ groups (`omarchy commands --all`, `omarchy debug`,
  `omarchy theme set`, `omarchy bar put`); 574 `omarchy-*` scripts are on PATH.
- Claude Code: `~/.claude/{CLAUDE.md,agents,commands,hooks,docs,keybindings.json,reference-docs,statusline-command.sh}`
  are symlinks into `~/Work/claude-code-config`. `~/.claude/settings.json` is a
  per-machine real file. A second copy of the repo exists at
  `~/projects/claude-code-config` (created 2026-11-08 and copied back to
  `~/Work` the same day, then deleted); `~/Work` is the only checkout. Other
  agent homes: `~/.codex`, `~/.config/Codex`, `~/.config/opencode`, `~/.gemini`,
  `~/.hermes`.
- Omarchy ships three agent skills in `/usr/share/omarchy/default/agents/skills`:
  `omarchy` (desktop and `~/.config` customisation, required for any such
  edit), `omarchy-app` (Qt app scaffold) and `diagnose-crash` (coredumps). Codex
  gets all three via `~/.agents/skills`. For Claude, `omarchy-app` was linked on
  2026-11-06 and `omarchy` + `diagnose-crash` were linked by hand into
  `~/.claude/skills` on 2026-11-08, because `install/core.sh` deliberately
  skips those two names (#694). The installer leaves foreign links alone, so
  they survive `./install.sh --core`.

**Installed software** (pacman: 1294 packages, 236 explicit, 1 AUR
`cellar-hifi`; no Flatpak; repos are Omarchy stable only, `pkgs.omarchy.org`
and `stable-mirror.omarchy.org`)
- Omarchy apps: hype, monologue, omacut, omawrite, omasnap, omacalc, owe +
  owe-lockfeed, signed, tobi-try, gliff, cliamp, herdr, omarchy-settings,
  omarchy-hyprland-titlebars, quickshell.
- Desktop: Chromium (default browser through `omarchy-browser`), 1Password +
  CLI, Obsidian, LibreOffice Fresh, Calibre, Kdenlive, OBS Studio, Pinta,
  Xournal++, Anki, Nautilus, Evince, gThumb, imv, mpv, Moonlight, LocalSend,
  Geary, Claude Desktop, Codex Desktop, Hermes Desktop, Zed, Proton VPN,
  NordVPN, Tailscale, Syncthing, system-config-printer/CUPS.
- Web apps (Chromium `--app`): Basecamp, Discord, Google Messages / Maps /
  Contacts / Photos, Hetzner Cloud, YouTube, Teams, WhatsApp, X, Tailscale,
  HEY, Zoom, and the local `sdlc` dashboard.
- Dev: git 2.56, gh, glab, lazygit, lazydocker, Neovim, tmux, Docker 29 +
  compose + buildx (daemon inactive; socket needs the `docker` group), Podman 6
  + podman-compose, clang/llvm 23, cmake, ninja, JDK 21 and 27, .NET 10,
  Ruby 3.4, Lua 5.1 + luarocks, Qt 6 tools, tree-sitter, shellcheck, bats.
  Via mise: claude 2.1.319, codex 0.167.0, bun 1.3.14, node 26, python 3.14,
  uv 0.13; also amp, antigravity-cli, basecamp (lazy). No Rust or Go toolchain.
- AI / local inference: ollama (installed, not running), whisper-cpp with
  `whisper-server.service`, voxtype, omarchy-voice, ggml-vulkan.
- Security / system: ufw active (+ ufw-docker), sshd active, gnome-keyring,
  fprintd, gitleaks, osv-scanner, restic, smartmontools, snapper, zram-generator,
  bolt, fwupd, intel-ucode, android-tools.
- User services running: syncthing, omarchy-tailscale-receive, whisper-server,
  voxtype, omarchy-voice, cellar, acmd, owed, librepods, bt-agent,
  xps-nas-backup-tunnel, omarchy-crash-watch, omarchy-sleep-lock.
- System services running: NetworkManager + wpa_supplicant, tailscaled,
  bluetooth, bolt, cups, avahi, sshd, nordvpnd, proton VPN, thermald,
  intel_lpmd, power-profiles-daemon, upower, udisks2, sddm, systemd-resolved,
  systemd-oomd.

**Observed this boot**: the journal shows `sshd` failing to start six times
early in the boot before it came up (it is active now); nothing else recurring.

**Needs root on this machine (ask first)**: `dmidecode`, `smartctl` /
`nvme smart-log`, `snapper list`, `fwupdmgr refresh`, the Docker socket,
`pacman -S/-R/-U`, system-scope `systemctl`, anything under `/etc`, and
`journalctl` for the system journal.

## Code Quality Standards

### Python (uv + FastAPI)
- Use `uv` for dependency management and project setup
- Comprehensive type hints throughout
- Self-documenting variable names and strategic docstrings
- Follow SOLID principles with clean architecture

### TypeScript (Bun Runtime)
- Advanced TypeScript patterns for backend systems
- Proper error handling and input validation
- OWASP security guidelines
- Microservices-ready architecture

### Testing (NO EXCEPTIONS)
- **Unit tests**: Cover all business logic
- **Integration tests**: Validate component interactions
- **End-to-end tests**: Verify user workflows
- Authorization required: "I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME"

### CI Compatibility — tests must pass in local GitLab job containers
Repos run CI on the local-ci-cd platform: ephemeral **Linux/arm64 container VMs, running as
root, on minimal digest-pinned images, with no network**. Tests that only pass on a developer
Mac are defects. Write to this contract:

- **Root user**: permission-based fault injection (`chmod`, read-only files/dirs) silently
  no-ops as root. Inject failures through seams (interfaces, fakes, injected errors) instead.
  If a filesystem-permission test is unavoidable, guard it: skip with an explicit message when
  `euid == 0`.
- **Process semantics**: PID 1 is a bare `sh` keep-alive — no init reaper, container-specific
  session/signal behavior. Tests that signal processes must kill the process *group*, escalate
  SIGTERM→SIGKILL, and wait with headroom; never assert a child died promptly.
- **Offline**: the platform's release gate runs with networking disabled. No test may reach the
  public internet; use local fixtures and fakes.
- **`localhost` is the job container**, never the host. Don't probe host services on 127.0.0.1.
- **Case-sensitive ext4** vs the Mac's case-insensitive APFS: paths/imports differing only by
  case fail only in CI.
- **Modest resources** (default 2 CPUs / 4 GB, sharing a host with GitLab): tight timeouts and
  wall-clock assertions flake. Prefer event-based waits over sleeps; give timeouts headroom.
- **Clean environment**: no dotfiles (`~/.gitconfig` etc.), no state from prior runs, only `sh`
  guaranteed (`bash` optional), `git` from the image or the platform's toolbox volume.
- **Triage rule**: a job failing with the *system-failure* exit code is a platform problem; a
  *build-failure* exit code is your code or tests. Don't relabel one as the other.
- **Local repro before blaming CI**: run the suite inside a container on the pinned job image
  (root + Linux + case-sensitive fs reproduces most of the above).

## Workflow & Agents

Story-driven development (`/generate-epics`, `REQUIREMENTS.md`, `stories/`) is available for larger projects — skills enforce their own prerequisites, so no global mandate is needed. See `WORKFLOW-v2.md` for the full multi-agent development lifecycle.

### Verifiable goals
For multi-step tasks, state a brief plan with explicit verification per step:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Strong success criteria let agents loop independently. Weak criteria ("make it work") require constant clarification. Transform fuzzy asks into verifiable goals: "Add validation" → "Write tests for invalid inputs, then make them pass". "Fix the bug" → "Write a test that reproduces it, then make it pass".

**Agents** (defined in `agents/*.md`): backend-typescript-architect, python-backend-engineer, ui-engineer, podman-container-architect, bash-zsh-macos-engineer, senior-code-reviewer, qa-engineer.

**Key skills**: `/brainstorm`, `/generate-epics`, `/resume-build-agents`, `/create-issue`, `/coverage`, `/create-project-summary-stats`.

**Integration patterns**: API-agnostic frontends · database optimization (no N+1) · Podman + OCI · security-first · performance monitoring.

## Piloting the SDLC

`sdlc` is the controller that owns the build and fix pipelines; the skills are thin wrappers around it. Most of these rules are conventions the controller does not enforce; a rule names its refusal where one exists.

- **Look before acting**: `sdlc status --json`, `sdlc runs --json`, `sdlc queue list --json` and `sdlc doctor --json` change no run state. Read them before starting anything.
- **Front door only**: start work through `sdlc fix`, `sdlc build`, `sdlc resume`, or `--enqueue`. Never call the `run-*` internals.
- **Never nest**: `SDLC_BATCH_BUILD` set means you are a dispatched agent. Return your `<<<RESULT_JSON>>>` block; do not start a run. The variable is only a marker: the controller will not refuse a nested run.
- **One live run per repo**: check `sdlc runs --json` first. `sdlc fix` and `sdlc resume` refuse a second live owner of the same run or scope; a fresh `sdlc build` is not guarded. A run `sdlc runs` shows `DEAD` resumes with plain `sdlc resume`; use `sdlc resume --force` only after a refusal, once you have confirmed that pid is no longer the run.
- **Long runs are not tool calls**: `build` and `fix` run for hours. Poll with `sdlc status`; never block on them.

## CLI Tools — Prefer installed utilities over builtins
The following tools are installed and SHOULD be used via Bash when the built-in Claude Code tools (Read, Grep, Glob, Edit) are insufficient or when working in shell scripts, pipelines, or subagents:

| Instead of | Use | Why |
|------------|-----|-----|
| `find` | `fd` | Faster, respects `.gitignore`, sane defaults. E.g. `fd '\.py$'` instead of `find . -name '*.py'` |
| `grep` / `rg` (via Bash) | `rg` (ripgrep) | Already installed — use when Grep tool can't cover the need (e.g. complex piped workflows) |
| `cat` (for reading) | `bat` | Syntax highlighting, line numbers, git integration. E.g. `bat src/main.ts` |
| `cd` (manual navigation) | `zoxide` (`z`) | Jump to frecent directories. E.g. `z myproject` instead of `cd ~/dev/long/path/myproject` |
| Interactive file selection | `fzf` | Pipe any list into `fzf` for interactive filtering. E.g. `fd '\.ts$' \| fzf` |
| `ls -la` for file browsing | `yazi` (`y`) | Full terminal file manager with previews. Available as `y` shell function |
| `jq` for JSON | `jq` | Installed — use for JSON processing in shell pipelines |
| PDF text/info extraction | `pdftotext`, `pdfinfo` | Poppler utils — extract text, metadata, images from PDFs. E.g. `pdftotext doc.pdf -` for stdout |
| `wc -l`, manual LOC counting | `scc` | Fast code counter with complexity and COCOMO estimates. **Always use `scc` for any LOC counting task.** E.g. `scc .` or `scc src/` |
| PDF generation (`reportlab`, `weasyprint`, etc.) | `typst` | Modern typesetting → PDF. **Always use `typst` for PDF generation.** Write a `.typ` file then `typst compile file.typ`. No Python libs or venvs needed |

**Rules:**
- Prefer Claude Code's built-in tools (Read, Grep, Glob, Edit) for direct file operations — they give the user better visibility
- Use these CLI tools via Bash when you need shell pipelines, complex filtering, or when the built-in tools are too limited
- In shell scripts and automation, always use `fd`/`rg`/`bat`/`jq` over legacy alternatives

## Source Control — local GitLab is master

Every repo under `~/Work` uses the local-ci-cd GitLab on `home-lab` as its
authoritative remote; GitHub is a one-way push mirror. Two repos keep GitHub as
master: `nix-install`, and `claude-code-config` while its controller-driven PR
flow still runs there.

- **Reach GitLab as `http://gitlab.test`** (port 80 via `tailscale serve` +
  split DNS) and set `GITLAB_HOST=gitlab.test` for `glab`. Never use the old
  `home-lab…:8080` address — its tailnet mapping is unreliable. A global git
  `insteadOf` rewrite still maps stale `:8080` remotes to `gitlab.test`.
- **`origin` is GitLab** (`http://gitlab.test/root/<repo>.git`); the `github`
  remote is only the mirror target. Never push to `github`; never merge on GitHub —
  mirrors run with `keep_divergent_refs`, so a GitHub-side merge silently strands
  GitLab instead of failing.
- **Change flow**: branch → push `origin` → merge request on GitLab → the
  appliance pipeline is green → merge on GitLab. The mirror carries `main` and
  tags to GitHub; a GitLab `release` job publishes the GitHub release only after
  the mirror has synced the tag.
- **Issues and MRs live on GitLab — use `glab`.** `gh` stays for the two
  GitHub-master repos and for read-only mirror checks.
  Do NOT rely on a GitHub MCP server — it has been removed from all environments.
  `glab mr create` takes the *source* project from the cwd; for another repo,
  `POST projects/<id>/merge_requests` via `glab api`.
- **Every GitLab-master repo carries `.sdlc-forge.yaml`** (`forge: gitlab`,
  `gitlab_url: http://gitlab.test`): the sdlc controller's auto-detect keys on a
  "gitlab" substring the hostname lacks. Keep it.
- **Keep `.github/workflows/ci.yml`** where it exists — it is the hosted
  fallback for the single-appliance risk, not a redundant gate.
- **New projects start on GitLab.** Create the project there, with `origin` at
  `http://gitlab.test/root/<repo>.git`, and commit `.sdlc-forge.yaml`. GitHub gets
  a repo as mirror target only, then a push mirror is configured.
  Never create a new project on GitHub first.

Starting a project, mirror operations, credential rotation, and post-reboot
recovery of the appliance: `~/.claude/reference-docs/source-control.md`.

## Release Version Alignment
- Before creating or deploying a release tag, align the package release number with the GitHub tag: `nyx/package.json` version `X.Y.Z` must match Git tag `vX.Y.Z`.
- When bumping the package version, update the lockfile too, for example with `npm version X.Y.Z --no-git-tag-version` from `nyx/`, then create the matching `vX.Y.Z` tag after committing.
- Do not deploy a tag whose `nyx/package.json` version disagrees with the tag name.

## Commit Format

This repo enforces [Conventional Commits](https://www.conventionalcommits.org/) via
commitlint (`.commitlintrc.json`). The `commit-format` CI job runs
`commitlint --from origin/main --to HEAD` on every merge request or PR; it fails
if any of its commits violate the rules. Existing history on `main` is exempt.

**How to write a commit message:**
- Header: `type(scope): subject` — `type` is required and lower-case, `scope` is
  optional and lower-case, `subject` starts lower-case, has no trailing period,
  and the whole header is at most 72 characters.
- Allowed types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`,
  `perf`, `build`, `revert`. `feat` drives a MINOR bump, `fix` a PATCH; other
  types do not trigger a release (see Epic-05 release workflow).
- Body and footer are blank-line separated. A breaking change is flagged with a
  `BREAKING CHANGE:` footer (or `!` after the type), which drives a MAJOR bump.

Three concrete examples:

```
feat(release): conventional commits + commitlint on prs

Adds a commit-format CI job so the release workflow has reliable
semver signal.
```

```
docs: document the commit format in the source-control reference
```

```
fix(hooks)!: drop unused stdin in permission hooks

BREAKING CHANGE: the permission hook no longer reads stdin for the log subcommand.
```

## Shared memory

The `memory` MCP server is FX's memory, shared with Codex, Claude.ai, ChatGPT, Hermes and Claude Desktop.

- Before answering a question about FX's infrastructure, projects or earlier decisions, call
  `search` with `namespace` set to the repository name and `include_global` true.
- When a decision is made, call `add` straight away with type `decision`: the choice and the
  reason for it.
- When FX states something durable about their infrastructure, projects or setup, call `add`
  with type `fact`.
- When FX corrects how you behave, call `add` with type `preference`: what to do instead.
- Set `namespace` to the repository name for anything specific to this project; omit it for what
  is true everywhere. The repository name is the main checkout's directory in lowercase, so a
  worktree gives the repository's name; in a directory that is not a git repository, omit it.
- Never store a secret, token or password in a memory.

## Reference Materials
- **CLAUDE.md guide**: `docs/claude-md-guide.md` — structure, guardrails, maintenance, and how this file itself is organized
- **Python**: `docs/python-best-practices.md`
- **Database**: `docs/database-best-practices.md`
- **Containers**: `docs/container-best-practices.md`
- **Testing & TDD**: `docs/testing-best-practices.md`
- **Source Control**: `@~/.claude/reference-docs/source-control.md` — see also the **Commit Format** section above
- **Full Workflow**: `WORKFLOW-v2.md`

---
*Surgical Changes rules, Complexity check, and Verifiable Goals template adapted from [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT), itself derived from Andrej Karpathy's observations on LLM coding pitfalls.*
