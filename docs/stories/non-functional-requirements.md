# Non-Functional Requirements

These constraints apply across all epics. Every story must respect them; the CI workflow in Epic-02 enforces the testable ones.

## Performance

- **Install time**: end-to-end install on a fresh macOS or WSL2 machine completes in under 5 minutes, excluding optional `brew install` of CLI tools.
- **Skill dispatch latency**: a skill invocation reaches its first sub-agent within 3 seconds of user input.
- **Parallel cohort throughput**: on a 48 GB M3 Max, a cohort of 5 CRUD stories completes Stage 1 (build) within 15 minutes.
- **State write latency**: SQLite writes from the orchestrator do not block the orchestrator for more than 50 ms per write (use WAL mode, batched commits).
- **Release pipeline runtime**: the auto-release workflow on a push to `main` completes in under 90 seconds.

## Reliability

- **Resumability**: any failed `build-stories` run is resumable to within one stage of the failure point. A second invocation must idempotently skip completed stages.
- **State durability**: SQLite stage-history table is append-only. Deletes happen only through an explicit `sdlc-state prune` command (or its MVP one-liner equivalent in bash).
- **Graceful degradation**: cmux absence never blocks any skill. Telegram absence never blocks any skill. Both log a warning, not an error.
- **No silent failures**: every non-success agent return produces a structured log entry the orchestrator can read and CI can verify.
- **Worktree cleanup**: completed worktrees are torn down within 6 hours of merge. A scheduled sweeper in `cmux-stop.sh` reclaims any orphans older than that.

## Portability

- **Supported platforms in MVP**: macOS 13 or later (Apple Silicon and Intel) and Windows 10 or 11 via WSL2 (Ubuntu 22.04 or later).
- **Out of MVP**: native PowerShell, Linux desktop distros, ARM Linux servers.
- **Required runtime**: Bash 4 or later. macOS uses `/opt/homebrew/bin/bash` or `/usr/local/bin/bash` (Homebrew bash). WSL2 uses system bash.
- **Optional runtimes**: Python 3.11 or later (only for the external controller, Roadmap Epic-07). Node 20 or later (release tooling).
- **Path separators**: every script uses POSIX paths. Windows paths only appear inside WSL2-side scripts, which see `/mnt/c/...`.

## Security

- **No secrets in repo**: `.env` is gitignored. Only `.env.example` is committed. CI fails if `.env` ever appears in a PR diff.
- **Token handling**: `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, and `BROWSER_PATH` are read from env at invocation time, never logged, never echoed in script output.
- **MCP server allowlist**: only servers declared in `mcp/config.template.json` are spawned by the install. A CI step validates the template parses and only references known server packages.
- **Permission default**: `settings.json` ships `permissions.defaultMode: "auto"` with a prominent README callout explaining what this means and how to opt out by editing the file before first run.
- **High-risk file detection** *(Epic-08, Roadmap)*: changes to `**/auth/**`, `**/payments/**`, `**/migrations/**`, `**/.github/workflows/**`, `Dockerfile*`, `**/*.tf`, and `**/secrets/**` block merge until human approval.
- **Telegram notifications**: built via `jq -n` to avoid shell-injection and quoting bugs. No `parse_mode: Markdown` until Epic-08 ships proper escaping for adversarial inputs.

## Maintainability

- **Skill size cap**: no `SKILL.md` exceeds 500 lines. Helpers and prompt templates live in sibling files under the same skill directory.
- **Agent registry**: every `subagent_type=` reference in a skill resolves to a file under `agents/`. Enforced by the CI validator in Epic-02 Story 2.1-003.
- **Doc link freshness**: CI fails if any markdown link in a tracked `.md` file does not resolve on disk or via HTTP 200.
- **Commit format**: Conventional Commits enforced on PRs via `commitlint` (Epic-05 Story 5.1-001). Commit types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`, `perf`, `build`.
- **Single source of truth for shared skills**: the SDLC plugin's skills live in this repo. The Codex mirror in `nix-install` consumes them via a sync mechanism defined in Epic-07.

## Observability

- **Structured logs**: every `cmux-bridge.sh log` call includes `--source <skill-or-agent-name>` and a level (`info`, `success`, `warning`, `error`). No bare strings.
- **Run audit**: every `build-stories` run produces a run ID, queryable via SQLite (`sqlite3 .sdlc-state.db "SELECT * FROM runs WHERE id=?"`). In Epic-07 this becomes `sdlc-state show <run-id>`.
- **Telemetry boundary**: nothing is sent off-machine except to the Telegram channel the user configured in `.env`. No analytics, no phone-home.
- **Local logs**: `cmux-bridge.sh` writes failures to `~/.claude/logs/cmux-bridge.log` (created lazily). Rotation deferred until size becomes an issue.

## Documentation

- **README sections required**: install paths, supported platforms, opt-out of auto-permissions, hardware envelope, quick start, troubleshooting, "what this script touches on your machine" disclosure.
- **CHANGELOG**: every release tag has a CHANGELOG entry covering Added / Changed / Fixed / Deprecated / Removed / Security.
- **Onboarding doc**: `docs/onboarding.md` walks a new colleague from install to first merged PR in under 30 minutes of reading.
- **No em-dashes** in any documentation produced for the framework. Use commas, parentheses, colons, semicolons, or periods.
- **Versioned docs**: every release tag captures the docs state at that tag. Users of `v0.3.x` can read the docs for `v0.3.x` regardless of `main` state.

## Compatibility and Versioning

- **Semver**: framework version follows `MAJOR.MINOR.PATCH`. Breaking changes to plugin contracts (skill names, argument hints, hook payloads, agent registry) bump MAJOR. Additive skills and agents bump MINOR. Bug fixes and doc updates bump PATCH.
- **Plugin manifest version**: `.claude-plugin/marketplace.json` and `plugins/autonomous-sdlc/.claude-plugin/plugin.json` versions are kept in sync with the git tag. CI fails if they drift.
- **Deprecation policy**: deprecated skills emit a warning for one MINOR cycle before removal in the next MAJOR.

## Out-of-Scope NFRs (explicitly deferred)

- High-availability state (no master/replica replication of SQLite).
- Multi-tenant runs (one user, one machine, one repo at a time).
- Encryption-at-rest of the SQLite ledger (consider after roadmap Epic-08 lands).
- Localization (English only).
