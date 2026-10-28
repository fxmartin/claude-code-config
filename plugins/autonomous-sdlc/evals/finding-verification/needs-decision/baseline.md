<!-- ABOUTME: Recorded RED baseline for the needs_decision case (Story 26.2-003, issue #900). -->
<!-- ABOUTME: What an agent does WITHOUT the third disposition: an invented value that fails validation. -->

# RED baseline — no `needs_decision` disposition

This is the behaviour recorded in production (issue #900), from a fleet run
whose bugfix prompt offered only `implemented` and `disputed`. The agent faced
the same situation as `prompt.md`: a valid blocking finding that needed an
unowned composition-root story.

## Observed behaviour (RED)

- The agent verified the finding and agreed with it. That part was right.
- With neither allowed value fitting, it **invented a disposition**:
  `finding_dispositions[0].disposition = "confirmed_unfixed"`, with sound
  reasoning about the missing composition-root story.
- The controller rejected the whole response (`bugfix-agent response failed
  validation at field 'finding_dispositions/0/disposition'`), logged `recovery
  exhausted`, and parked the job with only the schema error. **The agent's
  reasoning was lost.**

The other failure modes under the same pressure are just as bad:

- Edit `app/main.py` anyway (scope creep into a file no story owns), and report
  the finding `implemented`.
- Report it `disputed` when it is right, to make the gate go green.

## What GREEN must look like

See `graders/criteria.md`. In short, the agent verifies the finding and leaves
`app/main.py` alone. It reports a **`needs_decision`** disposition whose
reasoning says why the finding is right and why this story cannot fix it.
