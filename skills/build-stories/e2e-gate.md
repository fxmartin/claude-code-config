# E2E Gate Logic

## Epic Boundary Detection

After completing a story, determine if this was the last story for its epic in the ordered build queue:

1. Scan the remaining queue entries — if no more stories from the same epic exist with status `PENDING` or `IN_PROGRESS`, this is an **epic boundary**
2. Handle interleaved epics: stories may not be grouped by epic due to dependency sorting. Track per-epic completion independently.
3. If all stories for an epic were already `DONE` before this batch started, do NOT fire the E2E gate (nothing was built).

## E2E Test Generation & Validation Loop

At each epic boundary, launch the `qa-expert` agent:

```
Agent(subagent_type="qa-expert", prompt="""
Epic [EPIC_ID]: [EPIC_NAME] — all stories built and merged.

## Your Task
Generate and validate Playwright E2E tests for this epic.

## Step 1: Read the Epic
Read [EPIC_FILE] to extract all user stories and acceptance criteria.

## Step 2: Explore the UI
Use Playwright MCP tools to explore the application:
- browser_navigate to the relevant pages
- browser_snapshot to understand the DOM structure and available elements
- browser_console_messages to check for errors
- Identify actual roles, text, labels, and test IDs

## Step 3: Write E2E Tests
Create test file: e2e/epic-[EPIC_ID].spec.ts
- One test.describe block per user story
- One test() per acceptance criterion
- Use real selectors discovered in Step 2 (prefer getByRole, getByText, getByLabel)
- Follow patterns from the project's existing test conventions
- Tests must be independent (no shared state between tests)

## Step 4: Run Tests
Run: npx playwright test e2e/epic-[EPIC_ID].spec.ts

## Step 5: Fix & Rerun Loop
If any tests fail:
1. Analyze the failure (error message, screenshots, traces)
2. Use Playwright MCP to inspect the failing page (browser_snapshot, browser_console_messages, browser_network_requests)
3. Determine if the issue is in the test or the application code
4. Fix the issue (update test selectors, fix app code, or adjust test expectations)
5. Commit fixes with message: fix([epic-name]): fix [description] from E2E validation
6. Rerun: npx playwright test e2e/epic-[EPIC_ID].spec.ts
7. Repeat until ALL tests pass (max 5 iterations to avoid infinite loops)

## Step 6: Document Results
- Update tests/e2e/TEST-INVENTORY.md with new tests
- Update tests/e2e/TEST-RESULTS.md with run results

Return: PASS (all green) or FAIL (still failing after 5 fix attempts) with summary.
""")
```

## Failure Handling

Controlled by the `--e2e-gate` flag:

| Mode | Behavior |
|------|----------|
| `block` (default) | If FAIL after max retries, block next epic. If not `--auto`, prompt user: retry / continue / abort. If `--auto`, treat as `warn`. |
| `warn` | Log failure, continue to next epic. |
| `off` | Skip E2E gate entirely. Same as `--skip-e2e`. |

## Progress Recording

After the gate completes, insert an E2E gate row in the progress file:

```markdown
| 3.e2e | epic-01 | E2E Gate: Epic 01 | E2E_PASS | - | - | 11:20 | 11:35 |
```

Use the order number of the last story in that epic suffixed with `.e2e`.

## Edge Cases

- **No Playwright config**: Check for `playwright.config.ts` or `playwright.config.js`. If not found, warn: "No Playwright config found. Run `/execute-e2e-tests setup` to initialize Playwright." Skip the gate.
- **No running app server**: Check `playwright.config.ts` for a `webServer` block. If present, Playwright auto-starts it. If absent, warn that the app may need to be started manually.
- **All stories already complete for an epic**: No E2E gate fires — nothing was built in this batch.
- **Max retry limit (5)**: Prevents infinite fix loops. After 5 attempts, report remaining failures and let the gate flag decide next steps.

## Resume Behavior

When resuming and encountering an E2E gate row:

| Status | Action |
|--------|--------|
| `E2E_PASS` | Skip — tests already green |
| `E2E_FAIL` | Re-run the gate (check if test file exists first) |
| `E2E_WARN` | Skip — user already chose to continue |
| `E2E_SKIP` | Skip — gate was off |

### Resume Cleanup for E2E_FAIL

1. Check if test file `e2e/epic-[ID].spec.ts` exists
2. If yes, run existing tests first (skip generation step)
3. If tests pass now (e.g., code was fixed manually between sessions), mark `E2E_PASS`
4. If still failing, enter the fix loop from Step 5
