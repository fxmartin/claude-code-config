# Epic 21: Controller Self-Containment & Harness Selection

> **Status: CODE-COMPLETE (2/3)** — 21.1-001 + 21.2-001 merged on `main`
> (2026-06-28, PR #239); 21.3-001 (installer puts the Codex adapter on PATH)
> PLANNED — deferred because it edits `install.sh` and so must clear the
> high-risk approval gate. Created 2026-06-28. Makes the `uv tool install`ed
> `sdlc` actually usable for cross-harness (Codex) builds outside the source
> checkout — surfaced while wiring a repo to run builds on Codex.

## Epic Overview

**Epic ID**: Epic-21
**Description**: Epic-20 made the pipeline *cross-harness* (a role can route to
Codex), but two gaps meant cross-harness routing only worked from the controller
**source checkout**, never from the PATH-installed tool five colleagues actually
run: (1) the controller loaded all four `config/*.yaml` via a source-tree-relative
path (`parents[2]/config`) that does not exist inside a `uv tool install`ed wheel,
so the installed `sdlc` silently found **no** registry (`default_registry_path()`
returned `None`) and any `--harness …=codex` / repo `.sdlc-harness.yaml` failed
fast with "missing registry"; and (2) the harness registry's top-level `default:`
was parsed only to validate it, then discarded — so there was no shipped, editable
"use Claude / use Codex" switch. This epic makes the controller **self-contained**
(it ships and finds its own config) and adds a **registry-level default-harness
selector** with a documented toggle, plus the installer change that makes a Codex
worker runnable end-to-end.

**Business Value**: Cross-harness builds (Codex) work from the installed `sdlc`
in any repo — no source checkout, no `uv run --project` workaround. A colleague
flips one documented line (or drops a per-repo file) to choose Claude or Codex.
The same packaging fix repairs a latent security regression risk: the high-risk
gate, adversarial-reviewers, and over-engineering lens all read from the same
broken path and were silently inert in an installed tool.

**Success Metrics**:
- The PATH-installed `sdlc` resolves the bundled harness registry (was `None`).
- `--harness …=codex` and a repo `.sdlc-harness.yaml` route Codex from the
  installed tool, with zero `claude` worker processes.
- The active harness is selectable by editing one shipped, commented line, with a
  documented redeploy-safe per-repo alternative.

## Stories

##### Story 21.1-001: Ship controller config inside the installed wheel

**User Story**: As a colleague running the PATH-installed `sdlc`, I want the
controller to ship and find its own config files, so cross-harness routing and the
gates work outside the source checkout.

**Priority**: Must Have
**Story Points**: 3
**Status**: ✅ COMPLETE (PR #239)

**Acceptance Criteria**:
- **Given** a `uv tool install`ed `sdlc` with the source tree absent **When**
  `default_registry_path()` runs **Then** it returns a real, existing path under
  the installed package (not `None`).
- **Given** the built wheel **When** inspected **Then** all four config files are
  bundled under `sdlc/config/` (harnesses, adversarial-reviewers,
  high-risk-patterns, overengineering-lens).
- **Given** an editable/source install (CI, `uv run`, tests) **When** the same
  loaders run **Then** they resolve from the package source — no regression to the
  CI risk-gate job, the merge-agent path, or the test suite.

**Technical Notes**: Relocated `controller/config/*.yaml` →
`controller/src/sdlc/config/` (single source of truth), mirroring how
`src/sdlc/schemas/` is bundled. Added `src/sdlc/config/*.yaml` to the wheel
`artifacts` in `pyproject.toml`. New `bundled_config_path(name)` resolves via
`importlib.resources` to a real `Path`, returning `None` (not crashing) when
absent. Repointed `role_routing` (`_config_file`/`default_registry_path`/
`default_reviewers_path`), `risk_gate.DEFAULT_CONFIG_PATH`, the test path
constants, `scripts/risk-gate-detect.sh`, and `tests/risk-gate.bats`. The
overengineering lens has no production default-path constant (its loader takes an
explicit path), so only its test path moved.

**Definition of Done**:
- [x] Config relocated into the package and bundled in the wheel (verified by
      unzipping the built `.whl`)
- [x] Installed tool resolves the registry; source/editable resolution unchanged
- [x] `scripts/risk-gate-detect.sh` repointed (it swallows its own exit via
      `|| true` in CI, so a missing config would have silently passed the gate)
- [x] Full suite green; ruff clean

**Risk Level**: Medium — touches config discovery shared by routing AND the
high-risk gate; the gate's `|| true` made a missed path a silent security
regression (caught in review). Landed `.py`/`.yaml`/`.toml`/`.md`/`.bats` plus the
one `.sh` repoint (risk-approved).

##### Story 21.2-001: Registry-level default-harness selector + documented toggle

**User Story**: As FX, I want the harness registry's `default:` to actually choose
the active harness for every role, switchable by editing one shipped line, so I can
make a machine default to Claude or Codex without passing `--harness` every time.

**Priority**: Must Have
**Story Points**: 3
**Status**: ✅ COMPLETE (PR #239)

**Acceptance Criteria**:
- **Given** `harnesses.yaml` `default: codex` and no flag/repo file **When**
  `sdlc build` runs **Then** every pipeline role routes to Codex.
- **Given** a `--harness` flag and/or a repo `.sdlc-harness.yaml` **When** combined
  with a registry default **Then** precedence is `flag > repo file > registry
  default > builtin claude`, role by role (no clobbering a flag/file-set role).
- **Given** `default: claude` or a missing registry **When** `sdlc build` runs
  **Then** behaviour is byte-identical to before (empty map → existing fast path).
- **Given** a malformed `default:` **When** `sdlc build` runs **Then** it exits 2
  with a message, not a traceback.

**Technical Notes**: `registry_default_harness()` reuses `load_harnesses_config`
for fail-fast validation then reads the `default` key. `cmd_build` calls
`apply_registry_default()` after `apply_repo_harness_defaults`, `setdefault`-ing
each `PIPELINE_ROLES` entry only when the default is truthy and `!= "claude"`.
`harnesses.yaml` ships `default: claude` active with a commented `# default: codex`
toggle and a note that the file is overwritten on `uv tool install --force` — for a
redeploy-safe per-repo switch, use `.sdlc-harness.yaml`.

**Definition of Done**:
- [x] Registry `default:` drives dispatch with the documented precedence
- [x] `claude`/missing registry is a true no-op; malformed default exits cleanly
- [x] Shipped commented claude/codex toggle + redeploy-clobber note
- [x] Tests cover precedence, no-op, fill-unmapped, clean-exit

**Risk Level**: Low — additive selector on top of the Epic-20 routing seam;
default path unchanged.

##### Story 21.3-001: Installer puts the Codex adapter on PATH

**User Story**: As a colleague enabling Codex in a repo, I want the Codex (and
Qwen) build adapter available on PATH automatically, so `sdlc build …=codex` runs
without a manual symlink.

**Priority**: Should Have
**Story Points**: 2
**Status**: ⏳ PLANNED

**Acceptance Criteria**:
- **Given** a fresh `install.sh` run **When** it completes **Then**
  `codex-build-adapter.sh` (and `qwen-build-adapter.sh`) resolve on PATH so the
  registry's bare-name command runs.
- **Given** an uninstall **When** it runs **Then** the adapter symlinks are
  removed.
- **Given** the symlink already exists **When** install re-runs **Then** it is
  idempotent.

**Technical Notes**: The registry command is the bare name `codex-build-adapter.sh`,
resolved on PATH at dispatch; it is currently **not** installed anywhere, so a
Codex run needs a manual `ln -sf "$PWD/scripts/codex-build-adapter.sh"
~/.local/bin/`. Automate it in the `--core` (or a dedicated) install mode, symlinking
the `scripts/*-adapter.sh` into `~/.local/bin` (where `uv` already places `sdlc`).
**This edits `install.sh`/`install/*.sh`, so the PR will trip the high-risk approval
gate and needs the `risk-approved` label** — which is why it is split from the
gate-free 21.1/21.2 work.

**Definition of Done**:
- [ ] Installer symlinks the codex (and qwen) adapter onto PATH, idempotently
- [ ] Uninstall removes them
- [ ] Documented in `docs/harness-adapters.md` (replace the manual `ln -sf` step)
- [ ] bats coverage for the install/uninstall symlink behaviour

**Dependencies**: 21.1-001 (installed tool now resolves the registry). None
blocking.
**Risk Level**: Medium — `.sh` change → high-risk gate; must be idempotent and not
disturb the existing `--core` symlink set.

## Epic Complete When
- The PATH-installed `sdlc` ships and resolves its own config; cross-harness
  routing works from any repo without a source checkout.
- The active harness is selectable via the registry `default:` (with the documented
  flag > repo file > registry default > claude precedence) and a redeploy-safe
  per-repo `.sdlc-harness.yaml`.
- The Codex build adapter is on PATH via the installer (21.3-001), so a Codex-only
  repo needs no manual symlink.
