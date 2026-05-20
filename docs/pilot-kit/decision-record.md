# Pilot Decision Record

**Story:** [6.3-001](../stories/epic-06-public-release-readiness.md#story-63-001-five-colleague-pilot-smoke-test)
**Owner:** FX (fills in post-pilot — this is intentionally a blank form right now).
**Source artifacts:** five returned [`feedback-template.md`](feedback-template.md) forms, the [`pilot-tracker.md`](pilot-tracker.md) ledger, the `pilot-feedback`-labelled GitHub issues, and the install transcripts.
**See also:** [`README.md`](README.md), [`docs/onboarding.md`](../onboarding.md)

This document is the **structured decision record** FX completes after the
pilot concludes. Until then, every field below is blank — no fabricated
verdicts, no optimistic assumptions.

---

## 1. Pilot summary (fill in post-pilot)

- **Pilot window:** YYYY-MM-DD to YYYY-MM-DD
- **Colleagues invited:** _ / 5
- **Feedback forms returned:** _ / 5
- **Platforms covered:** _ (e.g. `macOS-AS x 3, macOS-Intel x 1, WSL2 x 1`)
- **Total `pilot-feedback` issues filed:** _

---

## 2. Pass / fail acceptance criteria (epic-06)

Source of truth: `docs/stories/epic-06-public-release-readiness.md` story
6.3-001 acceptance criteria. Tick only after explicit verification — do NOT
pre-tick.

- [ ] Five LTM colleagues identified (at least one on Windows/WSL2, at least
      one on Intel Mac if available).
- [ ] Each colleague received the single onboarding message with the repo
      link, `docs/onboarding.md` link, install command, and friction-point
      ask.
- [ ] Each colleague was timed (informally) on install. **Target: under 15
      minutes including reading.**
- [ ] Each colleague ran `/brainstorm → /generate-epics → /build-stories
      epic-01 --sequential` on a fresh test repo.
- [ ] Every failure or friction point was filed as a GitHub issue with
      `pilot-feedback` and reproduction steps.
- [ ] A summary covering install times, friction points, feature requests,
      and the "would you use this for your own work" verdict (yes /
      yes-after-fixes / no) exists.
- [ ] **≥ 4 of 5 verdicts are "yes" or "yes-after-fixes" (score 4 or 5).**

---

## 3. Verdict distribution (fill in post-pilot)

Pulled from each returned feedback form's "Would you recommend it?" section.

| Score | Count | Colleagues (initials) |
|-------|-------|----------------------|
| 5 — yes, would use right now |   |   |
| 4 — yes after fixes          |   |   |
| 3 — maybe                    |   |   |
| 2 — no, not in current shape |   |   |
| 1 — no, concept doesn't work |   |   |

**Score ≥ 4 total:** _ / 5

---

## 4. Must-fix-before-public-release (fill in post-pilot)

Issues that block opening the repo publicly. Each one MUST have a GitHub
issue with a fix plan and an owner.

| # | Issue | Severity | Owner | Target |
|---|-------|----------|-------|--------|
|   |       |          |       |        |
|   |       |          |       |        |

---

## 5. Defer-to-post-MVP (fill in post-pilot)

Good ideas that surfaced but are not release blockers.

| # | Issue / feature request | Why deferred |
|---|-------------------------|--------------|
|   |                         |              |
|   |                         |              |

---

## 6. Go / no-go decision

> One of three. Pick exactly one post-pilot. Leave all three boxes empty until
> the verdict is settled — do NOT pre-pick.

- [ ] **GO — ship MVP as-is.** ≥ 4 of 5 said yes (score 4 or 5), no
      release-blocking issues, repo is opened publicly with the current
      `vX.Y.Z` tag. The deferred list (section 5) becomes a Post-MVP epic.
- [ ] **GO WITH CAVEATS — ship MVP after the must-fix list (section 4)
      lands.** ≥ 4 of 5 verdicts are positive but specific blockers must be
      addressed first. Tag a follow-up `vX.Y.Z+1` once the must-fix list is
      closed, then open the repo.
- [ ] **NO-GO — defer public release.** < 4 of 5 verdicts are positive,
      and/or the pilot surfaced an architectural blocker. Repo stays private,
      a new epic captures the rework required, second pilot wave scheduled.

**Decision date:** YYYY-MM-DD
**Signed:** FX
**Public-release tag (if GO):** `vX.Y.Z`
**Announcement message archived at:** _link to the colleague-announcement message_

---

## 7. Lessons learned (fill in post-pilot)

Short, blameless. Three buckets.

### What we should keep doing

-

### What we should change for the next pilot wave

-

### What surprised us

-

---

## 8. Follow-up actions

| Action | Owner | Due | Issue # |
|--------|-------|-----|---------|
|        |       |     |         |
