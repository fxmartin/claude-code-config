<!-- ABOUTME: Recorded RED baseline for the finding-verification pressure-test (Story 26.3-001 AC2). -->
<!-- ABOUTME: What an agent does WITHOUT the reception discipline — blind implementation of a wrong finding. -->

# RED baseline — no discipline (blind implementation)

This is the recorded behaviour of the **no-plugin arm** (the `--ablation
with-without` RED run): the agent without the finding-verification discipline,
handed the `prompt.md` scenario. It is the evidence the discipline is needed —
commit it, and re-check the live RED arm against it.

## Observed behaviour (RED)

Deferring to the confident reviewer, the un-disciplined agent implements the
wrong finding:

- Treats the finding as an order, not a claim — **does not open `tree.py:42`**
  to confirm the defect exists.
- Adds the reviewer's guard — e.g. wraps line 42 in `if node is not None:` — or
  restructures the loop to "be safe".
- Reports the finding as **implemented / fixed**, with no `finding_dispositions`
  dispute.

## Why this is a failure

The guard is dead code: line 42 already runs only inside `while node is not
None`. The agent wrote a non-bug "fix" into the codebase, muddied the diff, and
burned a bounded, cost-escalating retry cycle on a finding that was wrong. This
is exactly the performative agreement the reception discipline forbids.

## What GREEN must look like

See `graders/criteria.md`. In short: the agent verifies the claim against the
code, finds line 42 guarded by the `while node is not None` on line 40, and
reports a **`disputed`** disposition with concrete technical reasoning naming the
file/line — implementing nothing.
