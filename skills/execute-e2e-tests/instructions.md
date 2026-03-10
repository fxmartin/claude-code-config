# Playwright E2E Testing — Instructions

## Setup Steps

### 1. Install Playwright
```bash
# For npm projects
npm init playwright@latest

# Or manual install
npm install -D @playwright/test
npx playwright install
```

### 2. Configuration (`playwright.config.ts`)

Key settings to configure:
- `testDir`: where tests live (default: `./e2e` or `./tests`)
- `baseURL`: the app's dev server URL
- `webServer`: auto-start the dev server before tests
- `projects`: browser matrix (chromium, firefox, webkit)
- `retries`: 0 for local, 2 for CI
- `reporter`: `html` for local, `github` + `html` for CI

Minimal config template:
```typescript
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
  },
});
```

### 3. Directory Structure
```
e2e/
├── fixtures/          # Shared test fixtures and setup
│   └── base.ts        # Extended test with custom fixtures
├── pages/             # Page Object Models (if 5+ tests)
│   └── login.page.ts
├── auth.spec.ts       # Test files grouped by feature
├── navigation.spec.ts
└── global-setup.ts    # Global setup (auth state, etc.)
```

## Test Writing Patterns

### Basic Test Structure
```typescript
import { test, expect } from '@playwright/test';

test.describe('Feature Name', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/feature-path');
  });

  test('should do expected behavior', async ({ page }) => {
    // Arrange — setup is in beforeEach

    // Act
    await page.getByRole('button', { name: 'Submit' }).click();

    // Assert
    await expect(page.getByText('Success')).toBeVisible();
  });
});
```

### Locator Priority (best to worst)
1. `getByRole()` — accessible role + name (preferred)
2. `getByText()` — visible text content
3. `getByLabel()` — form labels
4. `getByPlaceholder()` — input placeholders
5. `getByTestId()` — data-testid attributes (stable but less semantic)
6. CSS/XPath selectors — last resort only

### Page Object Model (for larger suites)
```typescript
// e2e/pages/login.page.ts
import { type Page, type Locator } from '@playwright/test';

export class LoginPage {
  readonly page: Page;
  readonly emailInput: Locator;
  readonly passwordInput: Locator;
  readonly submitButton: Locator;

  constructor(page: Page) {
    this.page = page;
    this.emailInput = page.getByLabel('Email');
    this.passwordInput = page.getByLabel('Password');
    this.submitButton = page.getByRole('button', { name: 'Sign in' });
  }

  async goto() {
    await this.page.goto('/login');
  }

  async login(email: string, password: string) {
    await this.emailInput.fill(email);
    await this.passwordInput.fill(password);
    await this.submitButton.click();
  }
}
```

### Authentication State Reuse
```typescript
// e2e/global-setup.ts
import { chromium, type FullConfig } from '@playwright/test';

async function globalSetup(config: FullConfig) {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('http://localhost:3000/login');
  await page.getByLabel('Email').fill(process.env.TEST_USER_EMAIL!);
  await page.getByLabel('Password').fill(process.env.TEST_USER_PASSWORD!);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.context().storageState({ path: './e2e/.auth/user.json' });
  await browser.close();
}

export default globalSetup;
```

### API Mocking
```typescript
test('shows error on API failure', async ({ page }) => {
  await page.route('**/api/data', (route) =>
    route.fulfill({ status: 500, body: 'Server Error' })
  );
  await page.goto('/dashboard');
  await expect(page.getByText('Something went wrong')).toBeVisible();
});
```

## Running Tests

```bash
# Run all tests
npx playwright test

# Run specific file
npx playwright test e2e/auth.spec.ts

# Run tests matching name
npx playwright test -g "login"

# Run in headed mode (visible browser)
npx playwright test --headed

# Run with UI mode (interactive)
npx playwright test --ui

# Debug a specific test
npx playwright test --debug e2e/auth.spec.ts

# Show HTML report
npx playwright show-report
```

## Debugging Strategy

1. **Read the error message** — Playwright errors are descriptive
2. **Check screenshots** — `test-results/` contains failure screenshots
3. **Use trace viewer** — `npx playwright show-trace trace.zip`
4. **Use MCP browser tools** — navigate to the page, take snapshots, inspect elements
5. **Add `test.only`** — isolate the failing test
6. **Use `page.pause()`** — opens inspector mid-test (headed mode only)

## CI Integration

### GitHub Actions
```yaml
- name: Install Playwright Browsers
  run: npx playwright install --with-deps
- name: Run Playwright tests
  run: npx playwright test
- uses: actions/upload-artifact@v4
  if: ${{ !cancelled() }}
  with:
    name: playwright-report
    path: playwright-report/
    retention-days: 30
```

## Quality Checklist

- [ ] Tests are independent — no shared state between tests
- [ ] Tests use semantic locators (`getByRole`, `getByText`)
- [ ] No hardcoded waits (`page.waitForTimeout`) — use auto-waiting
- [ ] No flaky selectors (avoid nth-child, complex CSS chains)
- [ ] Sensitive data uses environment variables
- [ ] Tests clean up after themselves (created data, state)
- [ ] Each test has a clear, descriptive name
- [ ] Assertions use `expect` with specific matchers (not just `toBeTruthy`)
