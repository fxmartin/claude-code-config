# Windows Install Guide (WSL2-based)

This guide takes you from a fresh Windows 11 machine to a fully working
`claude-code-config` framework install. Every step runs inside WSL2 —
no Windows-native tooling is required beyond enabling WSL2 itself.

---

## Prerequisites

- Windows 11 (or Windows 10 version 21H2+)
- Administrator access in PowerShell
- An internet connection

---

## Step 1 — Install WSL2 with Ubuntu 22.04

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu-22.04
```

This installs WSL2 and Ubuntu 22.04 in a single command. Windows will prompt
you to reboot if the WSL2 kernel component is not already present. Reboot when
asked, then re-open PowerShell and rerun the command if the Ubuntu shell did not
open automatically.

> **If you already have WSL2** but not Ubuntu 22.04, run:
> `wsl --install -d Ubuntu-22.04` — it adds the distro without touching your
> existing ones.

---

## Step 2 — First-boot Ubuntu setup

After the Ubuntu 22.04 shell opens for the first time, you will be prompted to
create a UNIX user:

```
Enter new UNIX username: yourname
New password: ••••••••
Retype new password: ••••••••
```

Choose a username (lowercase, no spaces) and a password you will remember — you
will need it for `sudo` commands throughout this guide.

---

## Step 3 — Update the package index and install core tools

Inside the Ubuntu WSL2 shell:

```bash
sudo apt update && sudo apt upgrade -y

# Core utilities the installer depends on
sudo apt install -y \
    git \
    jq \
    sqlite3 \
    fd-find \
    ripgrep \
    bat

# fd and bat ship under different binary names on Ubuntu — create the aliases
# the framework expects
mkdir -p ~/.local/bin
ln -sf "$(which fdfind)" ~/.local/bin/fd
ln -sf "$(which batcat)" ~/.local/bin/bat

# Add ~/.local/bin to PATH if it is not already there
grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' ~/.bashrc \
  || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### Install the GitHub CLI (`gh`)

GitHub's official apt repository is the recommended source:

```bash
# Add GitHub CLI apt repo
(type -p wget >/dev/null || sudo apt install -y wget) \
  && sudo mkdir -p -m 755 /etc/apt/keyrings \
  && out=$(mktemp) \
  && wget -nv -O "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  && cat "$out" | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
  && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
     | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
  && sudo apt update \
  && sudo apt install -y gh
```

---

## Step 4 — GitHub authentication

```bash
gh auth login
```

Select **GitHub.com**, choose **SSH** as your preferred protocol, and follow the
browser-based flow (the CLI opens a URL you paste into your Windows browser).

Verify:

```bash
gh auth status
# expect: Logged in to github.com as <yourname>
```

---

## Step 5 — Clone the repo inside WSL2

Clone into your WSL2 home directory, **not** onto the Windows filesystem
(`/mnt/c/...`). Cloning on the Windows side causes severe I/O performance
degradation and occasional git lock-file issues.

```bash
mkdir -p ~/dev
git clone git@github.com:fxmartin/claude-code-config.git ~/dev/claude-code-config
cd ~/dev/claude-code-config
```

If you do not yet have an SSH key in your WSL2 home, generate one first:

```bash
ssh-keygen -t ed25519 -C "your@email.com"
gh ssh-key add ~/.ssh/id_ed25519.pub --title "WSL2-Ubuntu"
```

---

## Step 6 — Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values. The key variable on WSL2 is `BROWSER_PATH` — see
the [MCP mode section](#mcp-mode) below for details.

---

## Step 7 — Run the core installer

```bash
./install.sh --core
```

This symlinks the framework into `~/.claude/` (agents, skills, commands, hooks,
settings, and CLAUDE.md). No system packages are installed, no shell files are
modified.

Expected output ends with:

```
[core] Done. ~/.claude/ symlinks are in place.
```

---

## Optional modes

### Tools mode (CLI utilities via apt)

```bash
./install.sh --tools
```

On WSL2 the installer prefers `apt` for packages available there (`fd-find`,
`ripgrep`, `bat`, `jq`, `fzf`, `zoxide`). `yazi` is not in the Ubuntu apt
repos — the installer prints a one-line hint to install it via Cargo:

```bash
cargo install --locked yazi-fm yazi-cli
```

If you have Homebrew installed inside WSL2 and prefer it, pass `--prefer-brew`:

```bash
./install.sh --tools --prefer-brew
```

### MCP mode

```bash
./install.sh --mcp
```

The MCP mode merges `mcp/config.template.json` into `~/.claude.json`. It
requires `BROWSER_PATH` in your `.env` to point at a Chromium-based browser
reachable from WSL2.

For a Windows-side browser, use the `/mnt/c/` WSL mount path. Examples:

```bash
# Google Chrome
BROWSER_PATH="/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"

# Microsoft Edge
BROWSER_PATH="/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
```

The installer validates that the path starts with `/mnt/` or is a WSL2-side
binary and emits a clear warning if neither applies.

### Shell mode

```bash
./install.sh --shell
```

On WSL2, the shell mode appends to `~/.bashrc` (or `~/.zshrc` if zsh is your
default shell). It installs a `y()` function for `yazi` and a `dev()` stub:

```bash
dev() {
  echo "cmux is macOS-only; this command is a no-op on WSL2"
}
```

The stub is intentional — cmux does not run on WSL2 (see Known Limitations).

---

## VSCode integration

Install the **Remote - WSL** extension on the Windows side:

1. Open VSCode on Windows.
2. Go to Extensions (`Ctrl+Shift+X`) and search for `Remote - WSL`.
3. Install the extension published by Microsoft.

Then, from inside the WSL2 Ubuntu shell:

```bash
cd ~/dev/claude-code-config
code .
```

VSCode opens on the Windows desktop connected to the WSL2 filesystem. All
terminal sessions inside VSCode automatically run in the WSL2 shell.

---

## Verification

After running `./install.sh --core`, confirm the install worked:

```bash
# Symlinks should exist
ls -la ~/.claude/

# Expected entries (symlinks pointing into ~/dev/claude-code-config/):
# CLAUDE.md -> ...
# agents/ -> ...
# commands/ -> ...
# hooks/ -> ...
# skills/ -> ...
# settings.json -> ...

# If you installed Claude Code CLI:
claude --version
```

---

## Known limitations

| Feature | macOS | WSL2 |
|---------|-------|------|
| cmux sidebar pills, progress bar, ledger | Available | Not available (cmux is macOS-only) |
| `dev()` shell function | Opens cmux workspace | No-op stub (prints warning) |
| Telegram notifications | Available | Available — no platform dependency |
| `--tools` package manager | Homebrew | apt (brew with `--prefer-brew`) |
| `--shell` target file | `~/.zshrc` | `~/.bashrc` (or `~/.zshrc` if zsh is default) |

---

## Troubleshooting

### `apt` cannot find `yazi`

`yazi` is not in the Ubuntu 22.04 apt repositories. Install via Cargo:

```bash
# Install Rust toolchain if not present
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# Install yazi
cargo install --locked yazi-fm yazi-cli
```

### `BROWSER_PATH` is unreachable from WSL2

If your `BROWSER_PATH` points at a Windows-side browser, use the `/mnt/c/`
prefix. Find the correct path in Windows Explorer: navigate to the `.exe`,
right-click → Properties → note the location, then translate it:

```
C:\Program Files\Google\Chrome\Application\chrome.exe
→ /mnt/c/Program Files/Google/Chrome/Application/chrome.exe
```

Note the space in "Program Files" — quote the path in `.env`:

```bash
BROWSER_PATH="/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
```

### Git line-ending issues

If you cloned on the Windows filesystem first (or pulled files through Windows
tools), line endings may be CRLF. Fix globally inside WSL2:

```bash
git config --global core.autocrlf input
```

This ensures files checked out in WSL2 always have LF endings.

### `fd` or `bat` command not found

Ubuntu packages these as `fdfind` and `batcat` respectively. The Step 3
instructions create `~/.local/bin/fd` and `~/.local/bin/bat` symlinks. If they
are still missing, confirm `~/.local/bin` is on your `PATH`:

```bash
echo $PATH | grep -o "$HOME/.local/bin"
# if empty:
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### WSL2 clock drift

Long suspend/hibernate cycles on Windows can cause the WSL2 clock to drift,
breaking `https` certificate validation. Resync with:

```bash
sudo hwclock -s
```

---

## Tested with

| Component | Version |
|-----------|---------|
| WSL2 | Ubuntu 22.04 LTS (target platform) |
| Windows | 11 |
| Date verified (target) | 2026-05-20 |

> This guide documents the target configuration. Colleague review against a live
> WSL2 Ubuntu 22.04 box is pending before MVP pilot (DoD: reviewed by one LTM
> colleague).
