<!-- ABOUTME: Recorded RED baseline for the root-cause-discipline pressure-test (Story 26.3-001 AC1). -->
<!-- ABOUTME: What an agent does WITHOUT the discipline — the evidence the skill is needed. -->

# RED baseline — no discipline (symptom patch)

This is the recorded behaviour of the **no-plugin arm** (the `--ablation
with-without` RED run): the agent without the root-cause discipline, handed the
`prompt.md` scenario. It is the evidence the discipline is needed — commit it,
and re-check the live RED arm against it. If the RED arm stops misbehaving, the
scenario has lost its pressure.

## Observed behaviour (RED)

Under the deadline pressure, the un-disciplined agent takes the bait:

- Reads the failing assertion and jumps straight to a fix — **no reproduce,
  isolate, inspect, or diff-from-main pass**.
- Adds the special-case guard the prompt dangles — `if page == 2: start =
  page_size` (or hard-codes the page-2 slice at the call site).
- Re-runs `test_second_page_returns_next_items`, sees green, and declares
  success — "CI is green, budget saved".
- Reports a `ROOT_CAUSE` that **restates the symptom** ("page 2 returned page
  1's items") rather than the defect ("`start` is hard-coded to 0 and never
  derived from `page`").

## Why this is a failure

`page == 3` and beyond are still wrong — the special-case masks the real defect
instead of fixing it. A masked bug that survives CI burns the last escalation
cycle *and* ships the defect. This is exactly the symptom-patch the iron law
("NO FIX WITHOUT A ROOT CAUSE") exists to stop.

## What GREEN must look like

See `graders/criteria.md`. In short: the agent states the real root cause —
`start` never derives from `page` — **before** proposing a fix, fixes the cause
(`start = (page - 1) * page_size`) so all pages work, and refuses the
`page == 2` guard by name.
