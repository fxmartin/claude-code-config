<!-- ABOUTME: Quick-start for the sdlc controller CLI package (Epic-07). -->
<!-- ABOUTME: Covers install via uv tool install and the planned subcommands. -->

# sdlc-controller

External controller for the autonomous-SDLC state machine. It owns the
orchestration logic that previously lived inside a Claude skill prompt, so
control flow is deterministic Python instead of an LLM interpreting a markdown
playbook. See [ADR-001](../docs/adr/001-controller-runtime.md) for the runtime
decision (Python + uv + Typer + Pydantic).

## Install

```bash
uv tool install .            # from this controller/ directory
```

No uv yet? The repo ships a bootstrap wrapper that installs uv first:

```bash
./scripts/install-controller.sh   # from the repo root
```

## Usage

```bash
sdlc --version    # prints the release version (matches the git tag)
sdlc --help       # lists every planned subcommand
```

### Planned subcommands

| Command    | Purpose                                              |
|------------|------------------------------------------------------|
| `init`     | Scaffold a controller workspace and SQLite ledger.   |
| `build`    | Run the full build-stories orchestration for a scope.|
| `resume`   | Resume an interrupted build from the ledger state.   |
| `status`   | Show the current run status and stage progress (`--json` for a snapshot). |
| `dashboard`| Serve a local auto-refreshing web view of build progress. |
| `state`    | Inspect the persisted state machine for a run.       |
| `validate` | Validate an agent response against its JSON schema.  |
| `rollback` | Roll a run back to a prior ledger checkpoint.        |

`build`, `status`, `dashboard`, and `validate` are implemented. The remaining
subcommands (`init`, `resume`, `state`, `rollback`) are stubs at this stage; they
print a "not yet implemented" notice and exit cleanly.

## Watching progress

A build is silent on stdout until it finishes, so progress lives in the SQLite
ledger. Two read-only ways to watch it (neither interferes with the running
build):

```bash
sdlc status                 # one-shot text snapshot (run + per-story + events)
sdlc dashboard              # local web dashboard → http://127.0.0.1:8787
sdlc dashboard --open       # …and open it in your browser
sdlc dashboard --restart    # replace a running dashboard (e.g. after upgrading)
sdlc dashboard --stop       # stop a (possibly backgrounded) dashboard
```

The dashboard (Catppuccin Latte theme) auto-refreshes — run summary, progress
bar, per-story stages, clickable PRs, recent events. A **left sidebar lists this
repo's past runs** (the ledger is per-repo) so you can click any run to inspect
it; "● Live" follows the newest. The **header names the GitHub project**
(`owner/repo`, linked). Binds **localhost only** by default (`--host`/`--port`/
`--run` to override). Runs until Ctrl-C.

## Agent I/O contracts (Story 7.2-001)

Each agent the orchestrator dispatches returns a JSON object fenced with
`<<<RESULT_JSON>>>` ... `<<<END_RESULT>>>` markers. The schemas live in
[`src/sdlc/schemas/`](src/sdlc/schemas/) (JSON Schema draft 2020-12), bundled in
the package so they ship in the installed wheel. `validate` parses the block
and validates it, surfacing the offending field on failure:

```bash
sdlc validate build agent-response.txt   # file, or pipe via stdin
cat resp.txt | sdlc validate coverage
```

See [`docs/contracts.md`](../docs/contracts.md) for the full contract.

## Agent dispatch (headless)

The controller dispatches each stage's agent as a headless `claude -p` subprocess.
Because there is no human to approve tool calls in that mode, the default command
passes `--dangerously-skip-permissions` so the agent can actually write files,
commit, and call `gh`. Override the whole command to tune the permission posture
per environment:

```bash
export SDLC_AGENT_CMD="claude -p --permission-mode acceptEdits --allowedTools Edit,Write,Bash"
```

Each agent's transcript (stdout + stderr) is saved under `<ledger>.logs/<run>/`
and its path recorded in the ledger (`stages.output_path`), on success and
failure, so a run is debuggable after the fact. The ledger files
(`.sdlc-state.db*`) are added to the repo's `.git/info/exclude`, so the
controller never dirties the repo it builds in.

## Development

```bash
uv sync --extra dev
uv run pytest
```
