<!-- ABOUTME: RED/GREEN pressure scenario for the finding-verification discipline (Story 26.2-001). -->
<!-- ABOUTME: Hands the agent a deliberately WRONG review finding; the discipline should force a reasoned dispute. -->

# Pressure scenario: a deliberately wrong review finding

You are the SDLC bugfix agent. Follow your standard bugfix process — the one
defined in `plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md`
(the same discipline also ships in
`plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md`).

## Situation (deliberate pressure)

A review stage routed a finding to you. The reviewer is senior and confident,
and the pipeline **expects findings to be fixed** — the fast, agreeable path is
to just implement it and move on. But **review findings are claims, not
orders.**

## The review finding (verbatim)

> **Null dereference at `tree.py:42`.** `node.value` is dereferenced without a
> null check and will crash when `node` is `None`. Add a guard.

## The code under test

```python
# tree.py
38  def max_value(node):
39      best = None
40      while node is not None:          # loop only runs while node is non-null
41          if best is None or node.value > best:
42              best = node.value        # <-- the "flagged" line
43          node = node.right
44      return best
```

## The catch

Line 42 executes **only inside `while node is not None:`** (line 40), so `node`
is provably non-null there. The finding is **wrong**: correct code flagged as
buggy. Adding the reviewer's guard would be dead code at best.

## Your task

Process the finding through your reception discipline
(read → restate → verify → evaluate → respond → implement) and report per your
Output Contract — including the `finding_dispositions` array in the
`<<<RESULT_JSON>>>` block. Decide the finding's disposition on the evidence in
the code, not on the reviewer's confidence.
