# Testing Best Practices

## TDD is Mandatory

**Red → Green → Refactor.** Write the failing test first, make it pass, then clean up.

Skip phrase (must be explicit): `"I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME"`

## Mandatory Tools: Python

| Tool | Purpose | Install |
|------|---------|---------|
| `pytest` | Test runner | `uv add --dev pytest` |
| `pytest-cov` | Coverage reporting | `uv add --dev pytest-cov` |
| `pytest-asyncio` | Async test support | `uv add --dev pytest-asyncio` |
| `httpx` | Async HTTP client for API tests | `uv add --dev httpx` |
| `factory-boy` | Test data factories | `uv add --dev factory-boy` |

## Mandatory Tools: JS/TS

| Tool | Purpose | Install |
|------|---------|---------|
| `vitest` | Test runner (Vite projects) | `bun add -d vitest` |
| `jest` | Test runner (non-Vite) | `bun add -d jest` |
| `@testing-library/*` | DOM/component testing | `bun add -d @testing-library/react` |
| `playwright` | E2E browser testing | `bun add -d @playwright/test` |
| `c8` / `v8` | Coverage via V8 engine | Built into vitest (`--coverage`) |

## Mandatory Tools: Shell

| Tool | Purpose | Install |
|------|---------|---------|
| `bats` | Bash test framework | `brew install bats-core` |

## Quality Gate Commands

| Stack | Command | Min Coverage |
|-------|---------|--------------|
| Python | `uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=90` | 90% |
| JS/TS (vitest) | `bunx vitest run --coverage --coverage.thresholds.lines=90` | 90% |
| JS/TS (jest) | `bunx jest --coverage --coverageThreshold='{"global":{"lines":90}}'` | 90% |
| Shell | `bats tests/` | N/A |

## Test Structure

### Python
```
tests/
  unit/           # Pure logic, no I/O, no DB
  integration/    # DB, APIs, external services
  e2e/            # Full workflows via HTTP client
  conftest.py     # Shared fixtures
```

### JS/TS
```
src/
  components/
    Button.tsx
    Button.test.tsx      # Co-located unit tests
tests/
  integration/           # API + DB tests
  e2e/                   # Playwright browser tests
```

## Naming Conventions

**Python**: `test_<what>_<condition>_<expected>`
```python
def test_create_user_with_duplicate_email_raises_conflict():
```

**JS/TS**: `describe` + `it("should ... when ...")`
```typescript
describe("UserService", () => {
  it("should throw ConflictError when email is duplicate", () => {
```

## E2E with Playwright

- **Page Object Model** — one class per page, encapsulates selectors and actions
- **Locator priority**: `getByRole` > `getByText` > `getByTestId` — never CSS/XPath
- **Independent tests** — no shared state, each test sets up its own data
- **No `sleep()`** — use `waitFor`, `toBeVisible`, or Playwright auto-waiting

## Coverage Rules

- **90% line coverage** minimum on all projects
- Focus coverage on: business logic, error paths, edge cases
- Measure changed files: `git diff --name-only main | grep '\.py$'` → run coverage on those
- Coverage is a gate, not a goal — don't write trivial tests to hit the number

## What to Test (Priority Order)

1. **Business logic** — core domain rules and calculations
2. **Error paths** — invalid input, missing data, permission denied
3. **Integrations** — DB queries, external API calls, message queues
4. **API contracts** — request/response shapes, status codes, headers
5. **UI behavior** — user interactions, form validation, navigation

## Anti-Patterns (Never Do These)

| Anti-Pattern | Why It's Bad | Do Instead |
|-------------|-------------|------------|
| Order-dependent tests | Flaky, impossible to run in isolation | Each test sets up and tears down its own state |
| Over-mocking | Tests pass but production breaks | Mock at boundaries only (DB, HTTP, clock) |
| Snapshot-only coverage | Catches regressions but not bugs | Assert specific behavior, use snapshots sparingly |
| `sleep()` in tests | Slow and flaky | Use polling, waitFor, or event-driven assertions |
| Testing implementation | Breaks on refactor | Test behavior and outputs, not internal calls |
| Ignoring flaky tests | Erodes trust in suite | Fix immediately or quarantine with a ticket |
