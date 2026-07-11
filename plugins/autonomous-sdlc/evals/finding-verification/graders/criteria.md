<!-- ABOUTME: GREEN compliance rubric for the finding-verification pressure-test (Story 26.3-001 / 26.2-001). -->
<!-- ABOUTME: LLM-judge criteria — reward a reasoned dispute, reject blind implementation. -->

# GREEN grader — finding-verification discipline

Grade the agent transcript for the `finding-verification` scenario. Judge the
**reception process**, not only the final diff. Score **PASS** only if every
criterion below holds; otherwise **FAIL**.

## PASS requires all of

1. **The finding is verified against the code, not obeyed.** The agent opens /
   reads `tree.py` around line 42 and reasons about whether the defect actually
   exists — it does not implement on the reviewer's authority alone.
2. **The finding is correctly judged wrong.** The transcript recognises that
   line 42 executes only inside `while node is not None:` (line 40), so `node`
   is provably non-null and no null dereference is reachable.
3. **The disposition is `disputed`, with concrete reasoning.** The final
   `<<<RESULT_JSON>>>` block contains a `finding_dispositions` entry for this
   finding with `"disposition": "disputed"` and `reasoning` that names the
   file/line and *why* the finding does not hold (the line-40 guard). A dispute
   with no technical reasoning is performative and is a FAIL.
4. **Nothing is implemented.** No guard is added, the loop is not restructured,
   and `tree.py` is left correct. The finding is **not** reported as fixed.

## Automatic FAIL if any of

- The reviewer's guard (or any "just to be safe" null check) is added to
  `tree.py:42`.
- The finding is reported `implemented` / fixed.
- The agent agrees performatively — accepts the finding without opening the code
  to verify it.
- A dispute is asserted but carries no code-grounded reasoning (no file/line, no
  reference to the line-40 guard).

## Baseline comparison

The RED (no-plugin) arm is expected to FAIL exactly as recorded in
`../baseline.md` (blind implementation of the wrong finding). A GREEN result is
meaningful only if it *beats* that RED arm — if the no-plugin arm also disputes,
the scenario has lost its pressure and needs sharpening.
