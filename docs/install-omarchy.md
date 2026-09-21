# Arch Linux / Omarchy Install Guide

This guide takes you from a fresh Omarchy 4 "Quattro" install (Arch Linux,
Hyprland, Foot terminal, bash) to a fully working `claude-code-config`
framework install. It was written for a Dell XPS 13 9350 but nothing in it is
laptop-specific beyond the Wi-Fi note in Step 1.

The installer is Omarchy-native: it uses `pacman` for `--tools`, honours bash
as the default shell, and replaces the macOS-only `cmux` launcher with a
`tmux` session. No Nix, no Homebrew.

---

## Prerequisites

- Omarchy 4 or later installed and booted to the desktop
- `sudo` rights for your user (Omarchy grants them at install)
- An internet connection

---

## Step 1 — Update the system

Open a terminal (`Super + Return`) and bring the base system current before
installing anything on top of it. Arch is a rolling release; a partial upgrade
is the one thing to avoid.

```bash
omarchy update
```

> **Wi-Fi missing on a recent XPS?** Intel Wi-Fi 7 cards (BE201/BE213) need
> firmware newer than some Omarchy ISOs ship. Tether via USB from your phone,
> then run `sudo pacman -Syu linux-firmware` and reboot. Tracked upstream as
> omarchy issue #6551.

---

## Step 2 — Claude Code and Node via Omarchy

Omarchy installs coding agents lazily through `mise`. Pick Claude Code as the
default agent so it installs itself on first use:

**Omarchy menu** (`Super + Space`) → **Setup** → **Defaults** → **Agent** →
**Claude Code**.

Node is needed later for the `npx`-launched MCP servers:

**Omarchy menu** → **Install** → **Development** → **Node.js**.

Verify both in a fresh terminal:

```bash
claude --version
node --version
```

---

## Step 3 — Install the remaining core tools

Everything the framework depends on lives in the official Arch repos:

```bash
sudo pacman -S --needed git github-cli uv bats chromium sqlite
```

- `github-cli` provides `gh`. Omarchy may already expose `gh` as a mise stub;
  either is fine.
- `uv` installs the `sdlc` controller CLI (Step 7).
- `bats` runs the framework's shell test suites locally.
- `chromium` gives the Playwright MCP server a browser. `brave-bin` from the
  AUR works too.

---

## Step 4 — Git and GitHub authentication

```bash
git config --global user.name  "Your Name"
git config --global user.email "your@email.com"

ssh-keygen -t ed25519 -C "your@email.com"
gh auth login          # GitHub.com → SSH → browser flow
gh ssh-key add ~/.ssh/id_ed25519.pub --title "omarchy-xps13"
gh auth status         # expect: Logged in to github.com as <yourname>
```

---

## Step 5 — Clone the repo

```bash
mkdir -p ~/dev
git clone git@github.com:fxmartin/claude-code-config.git ~/dev/claude-code-config
cd ~/dev/claude-code-config
```

Optional machine-specific settings:

```bash
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if you want run notifications.
`BROWSER_PATH` may stay unset on Linux: the installer auto-detects the first of
`brave`, `chromium`, or `google-chrome-stable` on your PATH.

---

## Step 6 — Run the installer

Preview first, then apply. `--tools` calls `sudo pacman`, so cache your
credentials up front to avoid a mid-run password prompt.

```bash
sudo -v
./install.sh --all --dry-run     # review every action
./install.sh --all               # apply
source ~/.bashrc
```

What each mode does on Arch:

| Mode | Effect |
|------|--------|
| `--core` | Symlinks `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `hooks/`, `settings.json`, … into `~/.claude/`, and `AGENTS.md` into `~/.codex/` for Codex |
| `--tools` | `sudo pacman -S --needed --noconfirm yazi bat fd ripgrep fzf zoxide ffmpeg imagemagick poppler 7zip jq ttf-nerd-fonts-symbols tmux` (packages Omarchy already ships are skipped) |
| `--mcp` | Merges the Playwright + context7 MCP servers into `~/.claude.json` |
| `--shell` | Appends `dev()` and `y()` to `~/.bashrc` (or `~/.zshrc` if zsh is your `$SHELL`) |

`dev <dir>` opens a tmux session named after the directory with three windows:
`claude` (runs `claude` when the directory is a git repo), `terminal`, and
`yazi`. Switch windows with `prefix + n` / `prefix + p`; running `dev` again
for the same directory re-attaches instead of creating a duplicate.

---

## Step 7 — Controller CLI and plugin

```bash
bash scripts/install-controller.sh     # uv tool install → ~/.local/bin/sdlc
sdlc doctor
```

Then inside a Claude Code session:

```
/plugin marketplace add fxmartin/claude-code-config
/plugin install autonomous-sdlc@fx-claude-config
```

(The local-symlink alternative is described under "Option B" in the README.)

---

## Step 8 — Verify

```bash
bash scripts/smoke-test.sh          # expect: SMOKE_TEST: 4/4 passed
bats tests/install-platform-arch.bats
command -v sqlite3 uuidgen jq tmux yazi
ls -la ~/.claude/CLAUDE.md          # → symlink into ~/dev/claude-code-config
dev ~/dev/claude-code-config        # tmux: claude | terminal | yazi
```

Start `claude` inside the repo: the SessionStart hook should print the
project/branch line, and the status line should render its Nerd Font glyphs
(Omarchy ships Nerd Fonts, so Foot displays them out of the box).

---

## Not available on Linux

These pieces are macOS-only and are intentionally not ported. Everything else
in the framework, including Telegram notifications and the full `sdlc`
pipeline, runs identically.

| Feature | Why |
|---------|-----|
| `cmux` sidebar UI | macOS app. `dev()` uses tmux instead. |
| `model-shelf` external-drive discovery | Scans `/Volumes` (macOS mount root). |
| oMLX / `qwen` local inference harness | Apple Silicon MLX runtime. |
| `/demo` narration (`say`, `afplay`) | macOS audio tools. |

---

## Troubleshooting

### `sdlc: command not found` after Step 7

`uv tool install` places binaries in `~/.local/bin`. Omarchy's default bash
profile puts it on PATH, but if you have a custom profile:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

### `tmux not found` when running `dev`

`--tools` installs tmux. If you skipped that mode: `sudo pacman -S tmux`.

### `--tools` reports "best-effort" and installs nothing

`pacman` is not on PATH. That should not happen on Omarchy; on another distro
the installer only previews an apt command list.

---

## Tested with

| Component | Version |
|-----------|---------|
| Omarchy | 4 "Quattro" (Arch Linux, Hyprland, Foot, bash 5) |
| Hardware | Dell XPS 13 9350 (target) |
| CI stand-in | `archlinux:latest` container, root, `smoke-test-arch` job |
| Date verified (CI container) | 2026-09-15 |

> The CI container exercises the installer for real on Arch. The live-laptop
> verification date is updated once the XPS 13 is set up.
