# E2E Test Design Rules

## Test Design Methodology

### Step 1: Parse Acceptance Criteria

For each story, extract acceptance criteria in the format:
- **Given**: precondition or initial state
- **When**: user action or trigger
- **Then**: expected outcome (the assertion)

If criteria are not in Given/When/Then format, convert them.

### Step 2: Identify Test Scenarios

For each acceptance criterion, design test scenarios:

1. **Happy path** — the criterion as stated
2. **Edge cases** — boundary values, empty inputs, max lengths
3. **Error paths** — invalid input, network failures, unauthorized access
4. **Cross-feature** — interactions with other stories in the same epic

### Step 3: Group into Test Suites

Organize test files by feature/story:
```
tests/e2e/
├── epic-01/
│   ├── story-01.1-001.spec.ts
│   ├── story-01.2-002.spec.ts
│   └── helpers/
│       └── fixtures.ts
├── TEST-INVENTORY.md
└── TEST-RESULTS.md
```

### Step 4: Write Playwright Specs

Each spec file should follow this structure:

```typescript
import { test, expect } from '@playwright/test';

test.describe('[Story ID]: [Story Title]', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the relevant page
  });

  test('[AC-1]: [acceptance criterion summary]', async ({ page }) => {
    // Given: setup preconditions
    // When: perform user action
    // Then: assert expected outcome
  });

  test('[AC-2]: [acceptance criterion summary]', async ({ page }) => {
    // ...
  });
});
```

### Step 5: Selector Strategy

Priority order for locators:
1. `page.getByRole('button', { name: 'Submit' })` — semantic, accessible
2. `page.getByText('Welcome')` — visible text
3. `page.getByLabel('Email')` — form labels
4. `page.getByPlaceholder('Enter email')` — placeholders
5. `page.getByTestId('submit-btn')` — data-testid (last resort before CSS)

**Never use**: raw CSS selectors, XPath, or positional selectors unless absolutely necessary.

### Step 6: Assertion Patterns

- **Visibility**: `await expect(element).toBeVisible()`
- **Text content**: `await expect(element).toHaveText('...')`
- **URL navigation**: `await expect(page).toHaveURL(/pattern/)`
- **API response**: intercept with `page.route()` or check `page.waitForResponse()`
- **Toast/notification**: wait for element, assert text, verify auto-dismiss
- **Form validation**: submit invalid data, assert error messages appear

### Step 7: Test Independence

Each test must:
- Set up its own state (use `beforeEach`, API calls, or fixtures)
- Clean up after itself if it creates data
- Not depend on other tests having run first
- Work in any execution order

## Quality Checklist

- [ ] Every acceptance criterion has at least one test
- [ ] Happy path, edge case, and error path covered
- [ ] No hardcoded credentials or sensitive data
- [ ] Locators use semantic selectors (getByRole/getByText)
- [ ] Tests are independent and order-agnostic
- [ ] Reasonable timeouts (no arbitrary waits)
- [ ] Test descriptions are clear and reference the AC they cover
- [ ] File structure follows project conventions

## Test Plan Format

Present the test plan as a table before writing specs:

| Story | AC# | Scenario | Type | Assertion |
|-------|-----|----------|------|-----------|
| 01.1-001 | AC-1 | User can log in with valid credentials | Happy | URL changes to /dashboard |
| 01.1-001 | AC-1 | Login fails with wrong password | Error | Error message shown |
| 01.1-001 | AC-2 | Session persists on refresh | Happy | User still logged in |
