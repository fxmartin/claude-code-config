# Playwright E2E — Interactive Questions

Ask these questions one at a time to assess the project and determine the best action.

## Question 1: Project Assessment

First, silently assess the project:
- Is there a `package.json`? What framework is used?
- Is Playwright already installed? (`@playwright/test` in devDependencies)
- Is there a `playwright.config.{ts,js}`?
- Are there existing E2E test files?

Based on assessment, skip to the relevant question:
- **No Playwright installed** → Go to Question 2
- **Playwright installed, no tests** → Go to Question 3
- **Existing tests** → Go to Question 4

## Question 2: Setup Needed

> Playwright is not set up in this project yet. Would you like me to:
> 1. **Full setup** — Install Playwright, create config, add a smoke test
> 2. **Config only** — Just create the playwright config (you'll install manually)
>
> Which option? (1 or 2)

After answer → proceed with setup from `instructions.md`

## Question 3: First Test

> Playwright is installed but there are no E2E tests yet. What would you like to test first?
>
> Describe a user flow (e.g., "user signs up and sees dashboard") or say **"smoke"** for a basic health check test.

After answer → proceed with test writing from `instructions.md`

## Question 4: Existing Tests

> I found existing E2E tests. What would you like to do?
> 1. **Run tests** — Execute the full test suite
> 2. **Write new test** — Add a test for a specific flow
> 3. **Debug failing test** — Investigate a specific failure
> 4. **Review coverage** — Analyze what's tested and what's missing
>
> Which option? (1-4)

After answer → proceed with the selected action

## Question 5: Confirmation

Summarize the planned action and ask:
> Ready to proceed? (yes / no / modify)
