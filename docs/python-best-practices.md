# Python Best Practices

## Mandatory Tools

| Tool | Purpose | Install |
|------|---------|---------|
| `uv` | Dependency management, project setup, script runner | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `ruff` | Linting + formatting (replaces black, isort, flake8) | `uv add --dev ruff` |
| `mypy` | Static type checking | `uv add --dev mypy` |
| `bandit` | Security vulnerability scanner | `uv tool install bandit` |
| `pytest` | Test runner | `uv add --dev pytest` |

## Quality Gates

Every commit must pass all gates. Run in this order:

| Gate | Command | Blocks on |
|------|---------|-----------|
| Type check | `scripts/run-type-check.sh` | Any violation beyond the baseline |
| Lint | `uv run ruff check .` | Any violation |
| Format | `uv run ruff format --check .` | Any diff |
| Security | `uv tool run bandit -r src/` | Medium+ finding |
| Tests | `uv run pytest` | Any failure |

## The Strictness Ladder (type checking)

The controller is checked by **mypy**, configured in `controller/pyproject.toml`.
A 27k-line tree cannot go red-to-green in one commit, so the rollout is
**ratcheted**: the known backlog is frozen and only *new* violations block.

| Rung | Applies to | Enforces |
|------|-----------|----------|
| **0 — floor** | every module | Real-defect checks: `check_untyped_defs`, `strict_equality`, `extra_checks`, `warn_redundant_casts`, `warn_unused_ignores`. Annotation coverage is *not* required. |
| **1 — strict** | the `[[tool.mypy.overrides]]` allowlist | Full `strict`: every def annotated, no implicit `Any` generics, no untyped calls, no `Any` returns. |

`ignore_missing_imports` is deliberately **off** — a blanket ignore turns whole
dependencies into `Any` and hollows out the gate. Stubs (`types-PyYAML`,
`types-jsonschema`) are pinned in the `dev` extra and resolved from `uv.lock`,
so the gate never fetches anything at check time.

### The ratchet

`controller/.mypy-baseline.json` records the accepted backlog as
`file|error-code|message → count`. It deliberately excludes line numbers, so a
baselined violation survives unrelated edits that shift it up or down the file.

```bash
scripts/run-type-check.sh            # gate: BLOCK on anything beyond the baseline
scripts/run-type-check.sh --update   # prune the baseline after draining backlog
```

- `CLEAN` — matches the baseline exactly.
- `WARN` — you *fixed* something baselined. Passes, but rerun with `--update`
  and commit the smaller baseline so the ratchet tightens.
- `BLOCK` — a violation the baseline does not cover. Exit 1.

**`--update` is for pruning, never for waving a fresh violation through.** The
baseline may only shrink; a PR that grows it needs an explicit reason in review.

### Moving a module up

The allowlist is how the ladder advances. When a module passes `strict`, add it:

```bash
cd controller && uv run mypy --strict src/sdlc/<module>.py   # must be clean
# then add "sdlc.<module>" to the [[tool.mypy.overrides]] module list
```

Grow that list; never shrink it. Note `strict = true` is a **global-only** flag —
setting it inside an override silently promotes the whole tree — so the
allowlist spells out the individual per-module flags instead.

## Project Setup

```bash
uv init <project>        # scaffold pyproject.toml
uv add <package>         # add dependency (updates uv.lock)
uv add --dev <package>   # add dev dependency
uv sync                  # install all deps into .venv
uv run <cmd>             # run inside the venv
```

**Never use bare `pip install`** — always `uv add` or `uv tool run`.

## Type Hints

- `from __future__ import annotations` at top of every module
- Use built-in generics: `list[str]`, `dict[str, int]`, `tuple[int, ...]`
- `TypeAlias` for complex types: `UserMap: TypeAlias = dict[str, list[User]]`
- All function signatures fully annotated — no untyped public APIs

## FastAPI Patterns

- `async def` for all route handlers
- Pydantic `BaseModel` for request/response schemas
- `Depends()` for dependency injection (DB sessions, auth, config)
- `HTTPException` with correct status codes for errors
- `Lifespan` context manager for startup/shutdown resources

## Architecture

```
src/<project>/
  api/          # Routes only — no business logic
  service/      # Business logic — orchestrates repositories
  repository/   # Data access — SQL, ORM, external APIs
  models/       # Domain models and Pydantic schemas
  core/         # Config, security, dependencies
```

- **SOLID principles** — single responsibility per module
- **No logic in routes** — routes call services, services call repositories
- **No N+1 queries** — use `selectinload` / `joinedload`, verify with SQL logging

## Error Handling

- Custom exception hierarchy inheriting from a base `AppError`
- Never bare `except:` — always catch specific exceptions
- Structured logging (`structlog` or `logging` with JSON formatter)
- Map domain exceptions to HTTP status codes in a single error handler

## Code Style

- Line length: 88 (ruff default)
- ruff replaces black + isort + flake8 — single tool, single config
- Self-documenting names over comments; comments explain **why**, not **what**
- Docstrings on public APIs only — keep them concise
