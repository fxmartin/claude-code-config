# Changelog

All notable changes to `sdlc-controller`. Format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning. History before 1.14.0 lives in the git log and the Epic-07/08
stories.

## [1.14.1] — 2026-06-18

Stops the controller from discarding completed, committed work when an agent's
result block drifts from the required format (R10). A real run fully implemented
and committed a story but emitted its result in a markdown ```json fence instead
of the `<<<RESULT_JSON>>> … <<<END_RESULT>>>` sentinels — the strict parser
rejected it, the run was marked FAILED, and coverage/push/PR never ran.

### Changed
- **Tolerant result parsing** — when the sentinel markers are absent, the parser
  falls back to the last ```json (or bare) fenced block, then the last balanced
  top-level JSON object. `parse_and_validate` picks the first *schema-valid*
  candidate, so an example/decoy object in the prose is skipped. A present but
  malformed sentinel block still raises its precise, actionable error.
- **Stronger prompts** — every rendered agent prompt now shows the exact result
  wrapper verbatim and states that markdown code fences are not accepted (belt
  and braces on top of the tolerant parser).

### Added
- **`NEEDS_ATTENTION` story status** — if a result is unparseable but the agent
  already committed the `feature/<story>` branch, the work is preserved (branch
  kept, a clear event logged, no bugfix-from-scratch, run not marked a clean
  success) instead of being thrown away. Surfaced in `status`/dashboard and the
  build summary; a non-clean run exits non-zero.

## [1.14.0] — 2026-06-17

Brings the controller fixes and the local dashboard developed in the
GitLab-native fork back into the GitHub-native controller. Together these make
`sdlc build <scope>` actually build (it previously could exit "0 done, 0 failed"
and ship nothing), give it a `status` command, and add a live web dashboard.

### Added
- **`sdlc status [--db --run --json]`** — reads the SQLite ledger **read-only**
  and prints run progress (summary + per-story table + recent events). With
  `--json` it emits one machine-readable snapshot so the build-stories skill can
  poll progress. `status_snapshot()` in `sdlc.build` is the shared payload used
  by both `status --json` and the dashboard.
- **`sdlc dashboard`** — a local, auto-refreshing web dashboard of build
  progress (stdlib `http.server`, no new deps). Serves `/` (an offline-safe HTML
  page that polls every ~2.5s: run summary, progress bar, per-story stages, PRs,
  recent events), `/api/status` (JSON snapshot), and `/api/runs` (this repo's run
  history). Reads the ledger read-only, follows the latest run, binds
  **localhost only** by default (`--host`/`--port`/`--run`/`--open`).
  - **Runs browser** — the sidebar lists past runs with status/scope/counts;
    click any run to inspect it, "● Live" follows the newest.
  - **Repo header + clickable PR links** — resolves the GitHub project web base
    from `git remote get-url origin` (ssh/https forms) and links each story's
    `#N` to `…/pull/N`; falls back to plain text when origin can't be resolved.
    Exposed in `/api/status` as `pr_base` and `project`.
  - **Catppuccin Latte** (light) theme and an "Autonomous SDLC" brand bar; the
    browser tab title is `<repo> · Autonomous SDLC`.
  - **`--stop` / `--restart`** — stop or replace a (often backgrounded)
    dashboard on a given host:port so upgrading the controller doesn't leave a
    stale server holding the port. A PID file is recorded per host:port (with an
    `lsof` fallback for older servers) and SIGTERM shuts down gracefully.
- **Single-story scope** — `build X.Y-NNN` (e.g. `build 34.5-003`) resolves the
  epic by its major number and queues exactly that story. (R2)
- **`--rebuild`** — rebuild stories the epic already marks Done. (R4)
- **`--preflight-timeout=SEC`** (default 600) — bounds the preflight gate and
  fails with a clear message instead of hanging. (R6)
- **`build --help`** documents every flag and all scope forms (`all`, `epic-NN`,
  `<name>`, `X.Y-NNN`) via a help epilog. (R1)
- **Configurable agent command** — override the dispatched agent command with
  the `SDLC_AGENT_CMD` env var. (R7)

### Changed
- **Headless dispatch actually works** — dispatched `claude -p` agents had no way
  to approve tool calls, so they committed nothing / opened no PR. The default
  agent command now passes `--dangerously-skip-permissions` so a headless agent
  can write files, commit, and call `gh`. (R7)
- **Transcript persistence** — each agent's stdout(+stderr) is written under
  `<ledger>.logs/<run>/` and its path recorded in `stages.output_path` on
  success **and** failure (persisted before validation, so a missing/invalid
  result block is still captured and debuggable). (R8)
- **Clean target repo** — the controller adds `.sdlc-state.db*` to the repo's
  `.git/info/exclude` (a local ignore that touches no tracked file), so the
  ledger never dirties the target repo's `git status`. (R9)
- **Preflight prefers the project's own gate** — `scripts/quality-gate.sh` or a
  `make gate` target — before any generic suite, adds `-n auto` to
  `uv run pytest` when pytest-xdist is present, streams output (no longer
  swallowed), and is time-bounded. A **dry run no longer runs preflight**. (R6)
- **Discovery reads `**Story Points**:`** in addition to `**Points**:`. (R5)
- **Shipped stories are skipped by default** — a story whose `**Status**:` starts
  "Done", or whose Definition-of-Done boxes are all checked, is recorded
  `SKIPPED` in the ledger (with an event) rather than rebuilt. A shipped
  dependency counts as satisfied and never blocks its dependents. `--rebuild`
  forces a rebuild. (R4)

### Fixed
- **A targeted scope that matches no stories now errors (exit 2)** with an
  actionable message, instead of reporting a hollow "0 done" success. `all`
  legitimately yielding an empty queue is left as exit 0. (R3)
