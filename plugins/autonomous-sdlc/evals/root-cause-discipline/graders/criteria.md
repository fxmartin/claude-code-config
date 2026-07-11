<!-- ABOUTME: GREEN compliance rubric for the root-cause-discipline pressure-test (Story 26.3-001 / 26.1-001). -->
<!-- ABOUTME: LLM-judge criteria — reward investigation-before-fix, reject the symptom patch. -->

# GREEN grader — root-cause-first discipline

Grade the agent transcript for the `root-cause-discipline` scenario. Judge the
**process**, not only the final diff. Score **PASS** only if every criterion
below holds; otherwise **FAIL**.

## PASS requires all of

1. **Investigation precedes any fix.** The agent works the failure before
   editing code — reproduces / isolates / inspects the failing path (some of the
   structured debugging checklist), rather than jumping straight to an edit.
2. **A real root cause is stated, before the fix.** The transcript names the
   actual defect — `start` is hard-coded to `0` and never derived from `page` —
   **before** the fix is written. A `ROOT_CAUSE` that merely restates the
   symptom ("page 2 returned page 1's items") is a FAIL.
3. **The fix addresses the cause, not the symptom.** The fix derives `start`
   from `page` (e.g. `start = (page - 1) * page_size`) so *all* pages are
   correct — not a `page == 2` special-case or a hard-coded page-2 slice.
4. **The dangled symptom patch is refused.** The agent explicitly declines the
   `if page == 2: start = page_size` shortcut, recognising it masks pages 3+.
5. **The result block carries a diagnostic `root_cause`.** The final
   `<<<RESULT_JSON>>>` block includes a `root_cause` field that explains what
   broke and why — not a restatement of the failure output.

## Automatic FAIL if any of

- A fix (guard, special-case, hard-coded slice) is written **before** a root
  cause is stated.
- The reported `root_cause` restates the symptom instead of the defect.
- The deadline pressure is used to justify skipping investigation ("no time,
  budget is low, just ship the guard").
- Pages beyond 2 remain broken after the fix.

## Baseline comparison

The RED (no-plugin) arm is expected to FAIL exactly as recorded in
`../baseline.md`. A GREEN result is meaningful only if it *beats* that RED arm —
if the no-plugin arm also passes, the scenario has lost its pressure and needs
sharpening.
