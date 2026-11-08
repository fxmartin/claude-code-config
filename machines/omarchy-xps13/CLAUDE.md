# omarchy-xps13 — Workspace Instructions

This file is `machines/omarchy-xps13/CLAUDE.md` in `claude-code-config`, linked
to `~/Work/CLAUDE.md` by `./install.sh --core` on this host only. It loads on
top of the global `~/.claude/CLAUDE.md` for every session started in `~/Work`.
Edit it in the repo and push to GitLab before and after, like any config.

## What `~/Work` is

- The session folder for running and maintaining the Dell XPS 13 (`omarchy-xps13`).
- The SDLC fleet worker's clone root: the controller resolves a job's repo as
  `~/Work/<name>`, so any other directory that appears here is a worker clone
  synced from its forge. Do not hand-edit those; they are reset on dispatch.
- `claude-code-config/` is the live config checkout. The `~/.claude` symlinks
  (CLAUDE.md, agents, commands, hooks, docs, keybindings, reference-docs,
  statusline) and the per-skill links under `~/.claude/skills` point into it.
- Human checkouts live in `~/projects/<name>`, not here (27 repos at the
  2026-11-08 survey). `~/Work` holds only this config checkout and worker clones.

## Maintaining the laptop from here

- Anything under `~/.config` (Hyprland Lua, `omarchy/shell.json`, Foot,
  starship, hooks, plugins, themes): load the `omarchy` skill first. Never edit
  `/usr/share/omarchy`; override in `~/.config` and validate with
  `hyprctl reload && hyprctl configerrors`.
- A process that crashed or vanished: load the `diagnose-crash` skill and start
  from `coredumpctl`.
- Diagnostics without root: `omarchy-debug --no-sudo --print` (the binary; the
  `omarchy debug` dispatcher rejects the flags on 4.5.0), `inxi -Fxxxz --no-sudo`,
  `hyprctl monitors`, `hyprctl devices`, `boltctl list`, `sensors`.
- Packages: `omarchy update` for the system, `omarchy pkg add` / `pacman` for
  packages, `yay` only for AUR. Never `brew`. User services via `systemctl --user`.
- Root is needed for `dmidecode`, `smartctl`, `nvme smart-log`, `snapper list`,
  the Docker socket, system-scope `systemctl`, `/etc`, and the system journal.
  Say which command and why before running any of them, then wait.
- Omarchy's own skills live in `/usr/share/omarchy/default/agents/skills`;
  `./install.sh --core` links them into `~/.claude/skills` and Codex reads them
  from `~/.agents/skills`.

## Machine Profile

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
  gets all three via `~/.agents/skills`. Since 2026-11-08 `./install.sh --core`
  links all three into `~/.claude/skills` as well (`link_distro_skills` in
  `install/core.sh`; the old #694 name-based exclusion of `omarchy` and
  `diagnose-crash` is gone). `--uninstall` removes only those links.

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
