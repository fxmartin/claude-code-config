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
| Type check | `uv run mypy .` | Any error |
| Lint | `uv run ruff check .` | Any violation |
| Format | `uv run ruff format --check .` | Any diff |
| Security | `uv tool run bandit -r src/` | Medium+ finding |
| Tests | `uv run pytest` | Any failure |

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
