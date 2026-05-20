# Pilot Kit — Five-Colleague Smoke Test

**Story:** [6.3-001](../stories/epic-06-public-release-readiness.md#story-63-001-five-colleague-pilot-smoke-test)
**Audience:** the five LTM colleagues piloting the `autonomous-sdlc` plugin.
**Time budget:** ~45 minutes total — 15 install + reading, ~25 first autonomous build, 5 to fill in the feedback form.

You volunteered (or were nudged into volunteering, sorry) to put this framework
on a real machine and tell me whether it survives contact with someone who
didn't build it. This page is the entire ask: three steps, one form.

---

## What's being piloted

A complete Claude Code configuration that runs `idea → /brainstorm →
/generate-epics → /build-stories` as a chain of autonomous agents and ships
merged PRs back to a repo. You are the first humans-other-than-FX to touch it.

We need to know three things:

1. **Does the install work on your box** (macOS Intel/Apple Silicon, Windows 11
   + WSL2 Ubuntu 22.04, anything else you bring to the table)?
2. **Does the autonomous build actually run** end-to-end on a real repo, or
   does it choke on something I never hit because I built it on my machine?
3. **Would you use it for your own work?** If not, why not — what's the
   blocker?

---

## What's expected of you

Exactly three things. Each has a clear deliverable.

### 1. One onboarding read-through

Read [`docs/onboarding.md`](../onboarding.md) start to finish. Stop when you
hit something that doesn't make sense, an instruction that's wrong, or a
prerequisite that's missing for your setup. **File a GitHub issue for each
friction point** with the `pilot-feedback` label. Don't sit on it — file as
you go.

Target: under 15 minutes of reading.

### 2. One `/build-stories` run on a fresh test repo

Create a brand-new throwaway repo (literally a fresh `~/dev/pilot-test/` or
similar — don't run the pilot on a repo you care about). Pick any
single-sentence project idea ("a CLI that converts CSV to JSON", "a markdown
linter that flags trailing whitespace", whatever).

Then run, in a fresh Claude Code session:

```text
/project-init "<one-line description>"   # if the repo is empty
/brainstorm
/generate-epics
/build-stories epic-01 --sequential
```

`--sequential` is slower but easier to follow on a first run — recommended.

What success looks like: epic-01's stories produce green PRs that get merged
to `main` on their own. Expected wall-clock: 20–45 minutes depending on the
project size, of which maybe 5 minutes is your active attention.

Capture anything that breaks. Screenshots are fine. Full Claude Code transcripts
are gold.

### 3. One feedback form

When you're done — pass, fail, or "I got fed up halfway and stopped" — fill in
[`feedback-template.md`](feedback-template.md). The form is a blank markdown
file: copy it, fill it in, send it back to FX (PR, gist, paste in chat, your
preference). It captures install time, blockers, what worked, what didn't,
and a 1–5 "would you recommend" score.

If you want the helper script to auto-capture your environment block (OS,
shell version, Claude Code version, etc.), run:

```bash
bash scripts/pilot-helper.sh
```

It prints a paste-ready markdown block; drop it into the **Environment**
section of the feedback form.

---

## What happens after

FX collects the five forms and fills in
[`decision-record.md`](decision-record.md):

- the pass/fail checklist (epic-06 acceptance: ≥ 4 of 5 say "yes" or
  "yes-after-fixes");
- the must-fix-before-public-release list;
- the deferred list (good ideas, not blockers);
- the go/no-go on opening the repo to the world.

Every `pilot-feedback` issue you filed is triaged — fixes that block public
release become Epic-06 follow-ups, the rest go to a Post-MVP epic.

---

## Ground rules

- **You're not testing your skill. You're testing the framework.** If you get
  stuck, that's a docs bug. File the issue.
- **Don't pre-clean the experience for me.** I want to see the rough edges; a
  pilot that returns "everything was perfect" is useless.
- **Skip with prejudice if needed.** If you genuinely cannot complete a step
  (e.g. `gh` won't authenticate on your corporate laptop), mark it skipped in
  the feedback form and move on. A partial pilot is more useful than no
  pilot.

---

## Links

- [`docs/onboarding.md`](../onboarding.md) — the walkthrough you'll read first
- [`docs/smoke-test.md`](../smoke-test.md) — automated install check (path A)
  and structural plugin check (path B)
- [`feedback-template.md`](feedback-template.md) — the form you fill in
- [`decision-record.md`](decision-record.md) — what FX produces after the pilot
- [`pilot-tracker.md`](pilot-tracker.md) — FX's running tracker (read-only for
  colleagues)
- GitHub issues with the `pilot-feedback` label: <https://github.com/fxmartin/claude-code-config/issues?q=label%3Apilot-feedback>
