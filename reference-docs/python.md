# Python Development Reference

## Package Management (uv)

- Use `uv` for all Python dependency management
- `uv init` to create new projects
- `uv add <package>` to add dependencies
- `uv sync` to install all dependencies
- `uv run <script>` to run scripts in the virtual environment

## FastAPI Patterns

- Use `async def` for route handlers
- Pydantic models for request/response schemas
- Dependency injection via `Depends()`
- Use `HTTPException` for error responses
- Lifespan events for startup/shutdown

## Type Hints

- All function signatures must have type hints
- Use `from __future__ import annotations` for forward references
- Prefer `list[str]` over `List[str]` (Python 3.10+)
- Use `TypeAlias` for complex type definitions

## Testing

- pytest as test runner
- pytest-asyncio for async test support
- httpx `AsyncClient` for API testing
- Factory pattern for test data generation

## Code Quality

- ruff for linting and formatting
- mypy for static type checking
- Follow SOLID principles
- Clean architecture: separate domain, application, infrastructure layers
