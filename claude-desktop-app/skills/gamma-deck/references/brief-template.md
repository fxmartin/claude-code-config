# Deck Brief Template

The brief is the structured output of the Stage 1 interview. It captures everything
needed to write the synopsis. Save it as markdown in the user's workspace.

## Format

```markdown
# Deck Brief: [Working Title]

**Created**: [date]
**Status**: Draft | Confirmed

---

## Strategic Intent

**Core message**: [The one thing the audience should walk away with]
**Triggering event**: [Why this deck needs to exist now]
**Desired outcome**: [What decision or action should follow this presentation]

---

## Audience

**Primary**: [Title, role, seniority — e.g., "VP Engineering, technical but time-poor"]
**Secondary**: [If applicable — e.g., "CFO will review slides async"]
**What they already know**: [Starting assumptions, prior context]
**What they're skeptical about**: [Objections to anticipate]
**Political context**: [Allies, competing priorities, organizational dynamics]

---

## Content & Narrative

**Story arc**: [The structural pattern — e.g., "Problem → Solution → Proof → Ask"]
**Key data points**:
- [Metric 1 — source, exact number]
- [Metric 2 — source, exact number]
- [...]

**Supporting evidence**: [Reports, benchmarks, customer quotes, demos]
**Explicitly out of scope**: [Topics the audience might expect but we won't cover]

---

## Format & Constraints

**Slide count**: [N slides]
**Time slot**: [Duration, if known]
**Tone**: [e.g., "Professional, data-driven, confident — no filler"]
**Visual style**: [Template name, color preferences, imagery preferences]
**Export format**: [Gamma link only / PPTX / PDF]

---

## Source Material

[List files, repos, documents, or conversation context used to build this brief]
- [file/source 1]
- [file/source 2]

---

## Open Questions

[Anything unresolved from the interview that might affect the synopsis]
- [Question 1]
- [Question 2]
```

## Filling the Template

Not every section needs to be filled for every deck. A quick internal update
might only need Strategic Intent, Audience, and Format. A client-facing pitch
needs everything.

**Rules:**
- If a section is empty because the user didn't provide info, mark it `[Not discussed]`
  rather than omitting it — this makes gaps visible.
- The "Open Questions" section is important. If something feels ambiguous after
  the interview, flag it here rather than guessing in the synopsis.
- "Source Material" creates the audit trail. If you read files from a repo or
  extracted data from uploaded documents, list them here so the user (or a future
  iteration) knows where the content came from.

## Confirming the Brief

After saving the brief, share it with the user and ask for confirmation.
Don't proceed to the synopsis until the user explicitly approves or the brief
has been adjusted to their satisfaction.

If the user says "looks good" or "go ahead", update Status to "Confirmed"
and proceed to Stage 2.

If the user has corrections, update the brief, re-share, and confirm again.
Don't iterate more than twice — if there are major changes, re-interview
on the specific areas that shifted.
