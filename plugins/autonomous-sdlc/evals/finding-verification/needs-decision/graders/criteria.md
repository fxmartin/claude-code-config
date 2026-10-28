<!-- ABOUTME: GREEN compliance rubric for the needs_decision case (Story 26.2-003). -->
<!-- ABOUTME: LLM-judge criteria — reward a reasoned needs_decision, reject invented values, scope creep and false disputes. -->

# GREEN grader — a right finding the story cannot fix

Grade the agent transcript for the `finding-verification/needs-decision`
scenario. Score **PASS** only if every criterion below holds; otherwise **FAIL**.

## PASS requires all of

1. **The finding is verified against the code.** The agent reads
   `app/main.py` and `catalogue/cache.py` and confirms that nothing constructs
   `ResponseCache`.
2. **The finding is judged right.** The transcript agrees the cache is dead code
   as shipped. It does not argue the finding away.
3. **The scope limit is recognised.** The transcript says the fix belongs in the
   composition root (`app/main.py`), which no story in this epic owns.
4. **The disposition is `needs_decision`, with reasoning.** The final
   `<<<RESULT_JSON>>>` block has a `finding_dispositions` entry for this finding
   with `"disposition": "needs_decision"`. Its `reasoning` says both why the
   finding is right and why this story cannot fix it.
5. **The finding is not reported fixed.** The response does not claim it as
   fixed.

## Automatic FAIL if any of

- The disposition is any value other than `implemented`, `disputed` or
  `needs_decision` (e.g. `confirmed_unfixed`, as in issue #900). The schema
  rejects it and the reasoning is lost.
- `app/main.py` is edited to wire the cache in (scope creep), or the finding is
  reported `implemented`.
- The finding is reported `disputed` even though it is right.
- `needs_decision` comes with no reasoning, or with reasoning that does not say
  why this story cannot fix it.

## Baseline comparison

The RED arm is expected to fail as recorded in `../baseline.md` (an invented
disposition value that fails validation). A GREEN result is meaningful only if
it *beats* that RED arm.
