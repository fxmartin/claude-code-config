# Install Performance Plan

**Status:** Proposal
**Date:** 31 August 2026
**Scope:** `install.sh` + `install/*.sh`, `scripts/install-controller.sh`, `docs/onboarding.md`, `README.md`
**Goal:** Cut clean-machine install wall time from ~10–20 min to ~2–3 min, close the
gate-scanner gap, and fix the onboarding sequence — without rewriting the installer.

---

## 1. Where the time actually goes

The installer scripts are not the bottleneck — `--core`/`--mcp`/`--shell` are
symlinks, a `jq` merge, and rc-file appends: seconds. Measured/verified on a
clean-machine profile:

| Step | Cost | Why |
|---|---|---|
| `brew install` in `--tools` | **~5–15 min — dominant** | 11 formulae + 1 cask, including **ffmpeg** and **imagemagick**, whose dependency trees pull dozens of bottles |
| `git clone` (Path B *and* marketplace Path A) | ~1–2 min | pack size is **115 MiB** (docx exports, screenshots, article assets in history) |
| First Claude Code session after `--mcp` | ~30–60 s hidden | playwright + context7 MCP servers are `npx`-fetched lazily — the cost moves into the first real session |
| `install-controller.sh` | ~30 s | uv bootstrap + 4 light deps (typer, pydantic, jsonschema, pyyaml) — fine as-is |
| `--core` submodule init | ~5–10 s | one submodule (`skills/model-shelf`), full history |

Two load-bearing facts:

- **ffmpeg, imagemagick, poppler, sevenzip, and the nerd-font are referenced by
  nothing in the pipeline.** They exist only for yazi's file previews (verified:
  no hook, script, or plugin references them).
- **`semgrep` and `osv-scanner` are installed by no mode**, yet `sdlc doctor`
  checks them and the SAST/depscan gates require them. A pristine box passes
  install and then fails doctor with no installer remedy.

## 2. Sequence review

The documented sequence (onboarding.md) is mostly right, with four defects:

1. **`cp .env.example .env` is demanded before `--core`**, but only `--mcp` (and
   Telegram) read it. The one manual, error-prone step is front-loaded before
   steps that need nothing. → Move it to the point of use.
2. **The controller is sequenced as optional/late**, yet `--core` symlinks the
   codex/qwen adapters into `~/.local/bin` *for* the controller, and the
   smoke-test section leans on `sdlc doctor`. → Standard sequence: clone →
   `--core` → controller → other modes → doctor.
3. **Gate scanners are in no step** (see above). → Add to `--tools` (§4, item 1).
4. **Path A (marketplace) likely ships a dead skill**: `--core` runs
   `git submodule update --init` for `skills/model-shelf`, but the marketplace
   clone path has no equivalent. → At minimum, a doctor check for an empty
   submodule-backed skill.

Target sequence after the fix:

```text
git clone --depth 1 --filter=blob:none …      # ~5 MiB instead of 115 MiB
./install.sh --core                            # symlinks (seconds)
./scripts/install-controller.sh                # sdlc CLI — no longer optional
./install.sh --tools                           # lean set incl. semgrep/osv-scanner
./install.sh --mcp                             # cp .env.example .env happens HERE
./install.sh --shell                           # optional
sdlc doctor                                    # final gate — everything green
```

## 3. Keep bash? Yes — for the bootstrap layer

The installer's job is to run on a machine that has nothing; a Python installer
inverts the bootstrap order (needs uv before it can install uv). The current
suite is modal, idempotent, dry-run-exact, and covered by 39 bats tests + CI
smoke tests on two OSes — paid-for reliability a rewrite re-buys at full price.

The real bash problem is **duplication**, not performance: `install/core.sh` and
`sdlc repair` maintain the same managed-symlink set in two languages. End-state
to head toward (later, not a prerequisite): bash keeps only what must precede
Python (clone, uv, `uv tool install controller/`); `sdlc install` absorbs the
modes — one source of truth, testable in Python.

## 4. Prioritized actions

Ordered by (time saved ÷ effort). Items 1–2 deliver most of the win.

### 1. Split `--tools` into a lean default + `--tools-media`

- `--tools`: `jq fd ripgrep fzf zoxide bat` + **`semgrep osv-scanner`**
  (what the pipeline and gates actually use)
- `--tools-media`: `yazi ffmpeg imagemagick poppler sevenzip` + nerd-font cask
  (yazi preview extras — cosmetic, opt-in)
- `--all` keeps both for backward compatibility; onboarding recommends the lean set.

**Win:** ~70% off the dominant brew cost; closes the scanner gap.
**Verify:** bats contract for the new flag; `sdlc doctor` CLEAN on a box that ran
only `--core --tools` + controller; `--all --dry-run` output covers both sets.

### 2. Shallow everything

- onboarding/README Path B: `git clone --depth 1 --filter=blob:none`
- `install/core.sh`: `git submodule update --init --depth 1`
- Longer term: evict article/screenshot media from the repo (or LFS) — it also
  bloats every marketplace (Path A) install, which clones the same 115 MiB.

**Win:** clone drops 115 MiB → a few MiB.
**Verify:** fresh shallow clone + `--core` + smoke-test 4/4; submodule skill
non-empty.

### 3. Parallel brew downloads

`export HOMEBREW_DOWNLOAD_CONCURRENCY=auto` in `install_tools_macos` (and the
`--prefer-brew` WSL2 branch). Free parallel bottle fetches; no behavior change.

**Verify:** dry-run output unchanged; bats green.

### 4. Pre-warm the MCP `npx` packages

During `--mcp`, background `npm cache add @playwright/mcp @upstash/context7-mcp`
(exact package specs read from `mcp/config.template.json`, not hard-coded) so the
first Claude Code session doesn't eat the lazy-fetch stall. Soft-fail: a missing
`npm` warns and skips — `--mcp` must keep working without Node (the config merge
itself needs only `jq`).

**Verify:** `--mcp` on a box without node still exits 0 with a warning; with
node, `npm cache ls` shows the packages.

### 5. Fix the documented sequence

Apply §2: reorder onboarding.md (clone → `--core` → controller → modes →
doctor), move `cp .env.example .env` into the `--mcp` step, promote the
controller out of "optional", update README to match.

**Verify:** docs-only; walk the sequence top-to-bottom on a scratch `$HOME`.

### 6. `--interactive` wizard with fetch-while-asking overlap

Today the installer asks no questions (flag-driven); the interactive moments
live in onboarding (edit `.env`, `gh auth login`, `/plugin` commands). A
`--interactive` mode overlaps network with Q&A:

```text
Phase 0 (background, parallel, each job → log file):
  brew fetch <formulae>                  # download-only; safe alongside anything
  git submodule update --init --depth 1
  curl uv installer → uv tool install controller/
  npm cache add <mcp packages>           # pre-warm npx

Phase 1 (foreground, meanwhile):
  Q: which modes?   Q: BROWSER_PATH?   Q: Telegram creds?  → writes .env

Phase 2 (join):
  symlinks + mcp merge + shell append    # seconds
  brew install <formulae>                # hits warmed cache — near-instant unpack
  wait every job; report per-job exit codes
```

Honesty constraints: two `brew install`s cannot run concurrently (lock) — the
sanctioned split is `brew fetch` then `install`; the apt path cannot background
`sudo` silently; every background job writes a log and is `wait`-ed with its
exit code checked — a swallowed failed download is worse than a slow install.

**Note:** smallest time win of the list — items 1–2 deliver most of the savings
with none of the job-control complexity. Do it for UX, not speed.
**Verify:** wizard run on scratch `$HOME` produces state identical to the
equivalent flag run; a killed background job surfaces as a named failure.

### 7. Later — consolidate into `sdlc install`

Fold mode logic into the controller, shrink bash to the bootstrap, delete the
`install/core.sh` ↔ `sdlc repair` duplication. Separate proposal when scheduled;
requires migrating the bats contracts.

## 5. Expected outcome

| Profile | Before | After (items 1–5) |
|---|---|---|
| Pilot (clone + core + controller + lean tools + mcp) | ~10–20 min | **~2–3 min** |
| First Claude Code session stall | 30–60 s | ~0 (pre-warmed) |
| `sdlc doctor` on fresh box | WARN/FAIL (scanners) | CLEAN |

Items 1–5 are surgical, independently landable, and each carries its own
verification. Item 6 is UX polish; item 7 is a scheduled refactor.
