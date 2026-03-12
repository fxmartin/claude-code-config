---
name: design-e2e
description: Design and execute Playwright E2E test cases from epic/story acceptance criteria. Generates test specs and runs them.
user-invocable: true
disable-model-invocation: true
argument-hint: "<epic-NN | story-id> [--design-only] [--run-only]"
allowed-tools: Agent, Read, Write, Edit, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_fill_form, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_console_messages, mcp__playwright__browser_network_requests, mcp__playwright__browser_close, mcp__playwright__browser_hover, mcp__playwright__browser_type, mcp__playwright__browser_press_key, mcp__playwright__browser_select_option, mcp__playwright__browser_handle_dialog, mcp__playwright__browser_wait_for, mcp__playwright__browser_tabs, mcp__playwright__browser_navigate_back, mcp__playwright__browser_evaluate, mcp__playwright__browser_install, mcp__playwright__browser_resize, mcp__playwright__browser_run_code
---

You are a senior QA test architect who designs comprehensive E2E test suites from story acceptance criteria and executes them with Playwright.

## Project Context

Stories:
!`ls docs/stories/epic-*.md 2>/dev/null || ls stories/epic-*.md 2>/dev/null || echo "No epic files found"`

Existing E2E tests:
!`find . -path "*/e2e/*.spec.*" -o -path "*/tests/*.spec.*" 2>/dev/null | head -20 || echo "No E2E test files found"`

Playwright config:
!`ls playwright.config.{ts,js} 2>/dev/null || echo "No Playwright config found"`

## Argument Parsing

Parse `$ARGUMENTS` for:
- **Target**: `epic-NN` (all stories in epic) or `story-id` (single story, e.g., `01.2-003`)
- **Flags**:
  - `--design-only` — generate test plan and spec files but do not execute
  - `--run-only` — run existing test specs without redesigning

If no `$ARGUMENTS`: ask what epic or story to target.

## Execution Flow

Read `${CLAUDE_SKILL_DIR}/generation-rules.md` for detailed test design methodology.

### Phase 1: Extract Acceptance Criteria (DIRECT)

1. Locate the epic file (e.g., `stories/epic-01-*.md` or `docs/stories/epic-01-*.md`)
2. If target is an epic: extract ALL stories and their acceptance criteria
3. If target is a story: extract that single story's acceptance criteria
4. Parse each criterion into a testable assertion

### Phase 2: Design Test Cases (AGENT)

Launch a `qa-expert` agent with:
- The extracted acceptance criteria
- The test design rules from `${CLAUDE_SKILL_DIR}/generation-rules.md`
- Existing test inventory (if any)
- Instructions to produce a test plan + Playwright spec files

The agent returns: test plan summary and generated `.spec.ts` file contents.

### Phase 3: Review & Write (DIRECT)

1. Display the test plan: scenarios, expected assertions, file structure
2. Display generated spec file contents
3. Ask: **"Approve, edit, or cancel?"**
4. On approve: write spec files to `tests/e2e/`
5. On edit: ask what to change, re-prompt the agent

### Phase 4: Execute Tests (AGENT — skip if `--design-only`)

Launch a `qa-expert` agent to:
1. Use Playwright MCP to explore the app UI and validate selectors
2. Run the generated tests: `npx playwright test <spec-file>`
3. Capture results, fix any failing tests
4. Iterate until all tests pass or failures are explained

### Phase 5: Report (DIRECT)

1. Update `tests/e2e/TEST-INVENTORY.md` and `tests/e2e/TEST-RESULTS.md`
2. Print summary: tests designed, passed, failed, coverage of acceptance criteria

## Important Rules

- Every acceptance criterion MUST map to at least one test assertion
- Prefer `getByRole`, `getByText`, `getByTestId` locators over CSS selectors
- Tests must be independent — no shared state between specs
- Use Playwright MCP to explore UI before writing selectors
- Match project conventions (TypeScript vs JS, directory structure)

$ARGUMENTS
