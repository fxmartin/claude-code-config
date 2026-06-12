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
| `status`   | Show the current run status and stage progress.      |
| `state`    | Inspect the persisted state machine for a run.       |
| `validate` | Validate an agent response against its JSON schema.  |
| `rollback` | Roll a run back to a prior ledger checkpoint.        |

Most subcommands are stubs at this scaffold stage (Story 7.1-001); they print a
"not yet implemented" notice and exit cleanly. Subsequent Epic-07 stories fill
in the behavior.

## Development

```bash
uv sync --extra dev
uv run pytest
```
