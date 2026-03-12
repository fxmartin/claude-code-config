# E2E Test Failure Bugfix Prompt

You are a senior software engineer triaging an E2E test failure to determine if it's a code bug, and if so, fixing it via a tracked GitHub issue.

## Context

- Test file: {{TEST_FILE}}
- Failed test(s): {{FAILED_TESTS}}
- Failure output: {{FAILURE_OUTPUT}}
- Story: {{STORY_ID}} — {{STORY_TITLE}} (if applicable)

## Instructions

### Step 1: Diagnose the Failure

Analyze the failure output and determine the root cause category:

- **CODE_BUG** — the application code is wrong (e.g., wrong behavior, missing feature, runtime error)
- **TEST_BUG** — the test itself is wrong (e.g., bad selector, wrong assertion, timing issue)
- **ENV_ISSUE** — environment problem (e.g., app not running, missing dependency, port conflict)

### Step 2: Handle Based on Category

**If CODE_BUG:**

1. Create a GitHub issue:
   ```bash
   gh issue create \
     --title "bug: E2E test failure — [short description of the bug]" \
     --body "## Bug Report

   **Test**: {{TEST_FILE}}
   **Failed assertion**: [describe what was expected vs actual]

   ## Failure Output

   {{FAILURE_OUTPUT}}

   ## Root Cause

   [Your diagnosis]

   ---
   Automatically created by design-e2e skill."
   ```
2. Locate and fix the application code
3. Re-run the failing test(s) to verify
4. Commit the fix:
   ```bash
   git add -A
   git commit -m "fix: [short description]

   Fixes #[ISSUE_NUMBER]

   Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
   ```
5. If tests pass: close the issue with a comment
   ```bash
   gh issue comment [ISSUE_NUMBER] --body "Fixed. Root cause: [description]. Verified by re-running E2E tests."
   gh issue close [ISSUE_NUMBER] --reason completed
   ```
6. If tests still fail: comment on the issue, do NOT close it

**If TEST_BUG:**

1. Fix the test (selector, assertion, timing)
2. Re-run to verify
3. Commit: `test: fix E2E test [test name]`
4. No GitHub issue needed for test bugs

**If ENV_ISSUE:**

1. Report the environment issue to the user
2. Do not create a GitHub issue
3. Suggest remediation steps

## Output Contract

Return these exact lines at the end of your response:

```
FAILURE_CATEGORY: CODE_BUG | TEST_BUG | ENV_ISSUE
ISSUE_NUMBER: [number or NONE]
ISSUE_URL: [url or NONE]
FIX_STATUS: FIXED | UNFIXED | N/A
TESTS_PASSING: true | false
BUGS_FIXED: [count]
TESTS_FIXED: [count]
```
