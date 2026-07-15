# fix-issue — Batch Mode Reference

<!-- ABOUTME: Batch-only reference for the fix-issue skill (Story 27.1-004).
     Loaded only when the target is `all` or `next` with `--limit=N`; the
     single-issue path never reads this file. -->

Batch mode changes nothing about how you invoke the controller: forward the
arguments verbatim (`sdlc fix all ...` / `sdlc fix next --limit=N ...`)
exactly as SKILL.md describes. This file only documents the batch-only flags
and what the controller does differently in a batch run.

## Batch targets

- `all` — every open issue: bugs first, then enhancements by priority.
- `next --limit=N` — the N highest-priority open bugs (`next` alone
  defaults to 1).

## Batch-only flags (forwarded to the controller)

- `--limit=N` — cap the issue set (`next` defaults to 1).
- `--sequential` — one issue fully completes before the next starts
  (issue-level concurrency of 1).
- `--concurrency=N` — issue-level worker cap (default 5).

## What the controller does differently in batch runs (reference only)

- **Investigate-first scheduling** — every issue is investigated before any
  build starts; issues that touch overlapping files are serialized while
  independent ones run concurrently (ready-queue scheduling, issue #436).
- **Single batch ledger run** — the batch records one run with scope
  `issues-all` / `issues-<n1>,<n2>`, so `sdlc dashboard` renders it like an
  epic build.
- **Batch doc-update** — after ≥1 issue merged, a single best-effort
  doc-update agent runs once at the end and opens a docs PR. Non-blocking:
  its failure never affects the batch's terminal status.
