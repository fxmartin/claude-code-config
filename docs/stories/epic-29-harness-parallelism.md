# Epic 29: Harness Parallelism Beyond Claude

> **Status: NOT STARTED (0/4)** — authored 2026-08-03. Thesis: the controller's
> parallel mode is gated on two capability flags (`parallel` +
> `worktree_isolation`), and today only the built-in Claude harness declares
> them. That gate is correct — N workers sharing one working tree would corrupt
> each other — but for the qwen and (future) opencode harnesses the `false`
> flags mean *never verified*, not *impossible*. Codex genuinely cannot earn
> worktree isolation (its per-directory trust/sandbox model does not cover
> ephemeral worktrees cut in temp dirs); Qwen Code and OpenCode have no such
> trust wall and plausibly pass with configuration alone. This epic verifies
> them, adds the missing opencode adapter, and flips the flags only on evidence.
>
> **Origin**: 2026-08-03 conversation tracing why `--parallel` degrades to
> serial on Codex (`degradation.py` PARALLEL_TO_SERIAL, `harnesses.yaml`
> capability declarations). A related defect found in the same investigation —
> the mode gate consults only the default-slot harness, never the per-role
> `--harness` map — is filed as its own bug issue, not absorbed here: it is a
> one-fix defect in existing behavior, independent of any new harness.

## Epic Overview

**Epic ID**: Epic-29
**Description**: The harness registry (Epic-20) made dispatch pluggable, but
parallel batch execution remains Claude-only because no other harness declares
the `parallel` and `worktree_isolation` capabilities the degradation matrix
(Story 20.5-002) requires. For Qwen Code the adapter already exists and the CLI
(a Gemini CLI fork, v0.21.2 on the reference machine) runs headlessly in any
cwd with an approval-mode flag for unattended writes — what is missing is
verification, not machinery. OpenCode (v1.18.10) is not registered at all, yet
its headless mode is the strongest non-Claude candidate: `opencode run` takes
`--dir` (arbitrary working directory), `-m provider/model` (slots into the
registry's `{model}` placeholder for per-stage routing, e.g. `openai/gpt-5.6`),
and `--format json` (a raw event stream that can later fund real usage
tracking). This epic (1) verifies Qwen Code unattended-in-a-worktree and flips
its flags, (2) adds an opencode adapter + registry entry serial-first, (3)
verifies and enables opencode parallelism the same way, and (4) optionally
parses opencode's JSON events so parallel cohorts are not cost-blind. Every
flag flip requires recorded evidence — the conservative-by-default rule
(`capability.py`: an undeclared capability is assumed absent) stays intact.
**Business Value**: FX's batch runs (`sdlc fix all`, multi-story builds)
currently serialize whenever routed to a non-Claude harness, turning a
20-minute parallel cohort into hours and making harness diversification
(Epic-20's point) costly to actually use. Verified parallelism on qwen and
opencode lets overflow work run concurrently on non-Anthropic quota when the
Claude Max rate-limit window is the binding constraint, and makes A/B harness
comparisons (Epic-11 evals) run at comparable wall-clock. The optional usage
parser closes the "cost recorded as unavailable" blind spot for whichever
harness ends up running real workloads.

**Success Metrics**:
- A batch run routed to qwen or opencode with `--parallel` executes with
  `effective_mode=parallel` (no PARALLEL_TO_SERIAL degradation event) and
  produces correct, non-colliding per-story branches from isolated worktrees.
- Zero regressions on the Claude path: capability resolution, degradation
  plans, and dispatch for the built-in harness remain byte-identical.
- (Stretch, 29.2-003) An opencode-routed stage records real token usage in the
  ledger instead of "unavailable".

**Out of Scope**:
- A `pi` adapter — the CLI is not installed on any target machine; revisit on
  demand.
- Codex parallelism — blocked upstream by its per-directory trust model, not
  by this controller; revisit only if Codex ships worktree-friendly trust.
- Rate-limit awareness (`rate_limit_aware`) for any non-Claude harness — none
  of these CLIs expose 429/reset semantics through their wrappers; parallel
  cohorts on API-key billing fail loudly rather than backing off, and that
  trade-off is documented, not engineered around, in this epic.
- The default-slot mode-gate defect (gate ignores `--harness role=…` routing) —
  filed as a standalone bug issue.

## Features in This Epic

### Feature 29.1: Qwen Code Parallel Enablement

The qwen harness entry and `scripts/qwen-build-adapter.sh` already exist
(Story 20.x); the CLI has no per-directory trust wall. This feature is pure
verification-then-flip: prove unattended writes inside a fresh git worktree,
record the evidence, and let the capability flags tell the truth.

#### Stories

##### Story 29.1-001: Verify and enable qwen worktree isolation + parallel
**Status**: Done
**User Story**: As FX routing batch work to Qwen Code, I want the qwen harness
to declare `worktree_isolation: true` and `parallel: true` once a recorded
smoke test proves `qwen -p` completes an edit task unattended inside a freshly
cut git worktree, so that `--parallel` cohorts routed to qwen actually run
concurrently instead of silently degrading to serial.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a fresh `git worktree` of a scratch repo **When**
  `qwen-build-adapter.sh` runs a small edit task in it with the approval mode
  required for unattended writes (no TTY, no interactive prompt) **Then** the
  task completes, the file change exists only in that worktree, and the
  invocation (flags + qwen version) is recorded in the story's evidence note.
- **Given** two such worktrees running adapter tasks concurrently **When**
  both complete **Then** neither worktree contains the other's changes and the
  shared repo root is untouched.
- **Given** the recorded evidence **When** the `qwen` entry in
  `controller/src/sdlc/config/harnesses.yaml` is updated **Then**
  `worktree_isolation: true` and `parallel: true` are set, the entry's comment
  cites the evidence (date + qwen version), and any approval-mode flag needed
  for unattended operation is baked into the adapter or documented in
  `QWEN_FLAGS`.
- **Given** the flipped flags **When** `preflight_harness` evaluates a
  `parallel` request for qwen **Then** the degradation plan contains no
  PARALLEL_TO_SERIAL entry (unit test), while `usage_unavailable` and
  `rate_limit_skipped` remain (unchanged).
- **Given** the smoke test fails (qwen cannot write unattended in a worktree)
  **Then** the flags stay `false`, the failure mode is documented in the
  harness entry comment, and the story closes as verified-negative rather than
  silently abandoned.

**Technical Notes**: Qwen Code is a Gemini CLI fork; check `--approval-mode`
/ `-y` (yolo) semantics for headless writes — the adapter currently passes
only `-p`. The dispatch layer already sets the subprocess cwd to the story
worktree, so no controller change is expected beyond the YAML flags; the work
is evidence + config + the preflight unit test. Keep the verification script
(scratch repo, two concurrent worktrees) under `controller/tests/` or
`scripts/` so the evidence is reproducible, but it must never run against the
real repo in CI (mirror the hermeticity guards in `controller/tests/conftest.py`).

**Definition of Done**:
- [ ] Recorded unattended worktree smoke test (single + concurrent) with qwen
      version and exact flags
- [ ] `harnesses.yaml` qwen entry flips `worktree_isolation` + `parallel` with
      an evidence-citing comment (or documents verified-negative)
- [ ] Unit test: qwen capability map yields no PARALLEL_TO_SERIAL degradation
- [ ] Adapter/`QWEN_FLAGS` documentation updated for the unattended approval flag

**Dependencies**: none
**Risk Level**: Medium

### Feature 29.2: OpenCode Harness

Add OpenCode as the fourth registered harness — serial-first with the same
conservative flags as every new entry, then verified parallel, then (stretch)
real usage telemetry from its JSON event stream. Registry design rule from
Epic-20 holds: config + wrapper script, no Python dispatch changes.

#### Stories

##### Story 29.2-001: OpenCode adapter and registry entry (serial)
**Field finding (2026-09-05, FX)**: this is **effectively done, ad-hoc**. A
working adapter and registry entry are live in `local-llm-tests/harness/`,
resolving through `resolve_harness` and validating through
`parse_and_validate`. Reconcile this story against that implementation rather
than building it a second time.

Two template defects found in the same session, both now fixed on `main`
(PR #624): the worked example documented `opencode run --quiet`, a flag that
does not exist in 1.18.15 (`run` prints help and exits), and OpenCode emits
ANSI even when stdout is not a TTY, which `parse_and_validate` rejects
outright. Either would have blocked a cold start on this story.

**Still unproven**: whether OpenCode emits `<<<RESULT_JSON>>>` for **every**
pipeline role. Only the build role is proven, via the self-test. This story's
"any pipeline role can be routed to OpenCode" claim rests on that assumption —
verify per role before closing it.
**User Story**: As FX with OpenCode installed and multi-provider models
configured, I want an `opencode` harness entry backed by
`scripts/opencode-build-adapter.sh` so that any pipeline role can be routed to
OpenCode (e.g. `openai/gpt-5.6`) exactly like the codex and qwen harnesses,
serial-first.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the assembled prompt on stdin **When**
  `opencode-build-adapter.sh` runs **Then** it invokes `opencode run`
  headlessly, forwards stdout verbatim so the `<<<RESULT_JSON>>>` block
  round-trips to the existing `codex-exec` parser, and exits non-zero on
  dispatch failure — mirroring the qwen adapter's contract, including a
  `--self-test` mode.
- **Given** the registry **When** the `opencode` entry is added **Then** it
  declares `parser: codex-exec`, `probe: "opencode --version"`, a
  `--model {model}`-ready command template documented like codex's (commented
  `models:` example using `provider/model` ids), and conservative
  capabilities: `json_contract: true`, everything else `false`.
- **Given** OpenCode's per-project permission config **When** the adapter runs
  unattended **Then** required `permission` settings (edit/bash allow) are
  documented in the adapter header and the harness entry comment — a headless
  build must never hang on an approval prompt.
- **Given** a repo with `.sdlc-harness.yaml` naming `opencode` **When** a
  build dispatches **Then** the stage runs on OpenCode with zero `claude`
  processes spawned (the Epic-20 AC3 pattern), verified by the harness routing
  line in the run log.
- **Given** the controller test suite **When** it runs **Then** adapter
  self-test and registry-resolution tests pass without invoking the real
  `opencode` binary (fake runner seam, as for codex/qwen).

**Technical Notes**: OpenCode 1.18.10: `opencode run [message..]` with
`-m/--model provider/model`, `--dir`, `--format json`. The adapter should pass
the prompt as the message argument or stdin (verify which survives large
prompts; qwen adapter uses `-p` with stdin append semantics). Do NOT use
`--format json` in this story — the plain formatted output with the prompt-
driven RESULT_JSON block keeps parser reuse trivial; the event stream is
29.2-003's business. Disable plugins (`--pure`) if plugin output can pollute
stdout. Model entitlement stays the user's problem, as with codex (issue #228
lesson: never hardcode a model id in the shipped template).

**Definition of Done**:
- [ ] `scripts/opencode-build-adapter.sh` with header docs, `--self-test`, and
      shellcheck-clean
- [ ] `harnesses.yaml` opencode entry (conservative flags, probe, model-routing
      example comment)
- [ ] Unattended `permission` config documented adapter-side and registry-side
- [ ] Tests: self-test contract, registry resolution, no-real-CLI hermeticity
- [ ] `docs/controller-architecture.md` harness table updated

**Dependencies**: none
**Risk Level**: Low

##### Story 29.2-002: Verify and enable opencode worktree isolation + parallel
**User Story**: As FX running parallel batches on OpenCode, I want the same
evidence-then-flip treatment qwen gets in 29.1-001 — a recorded unattended
worktree smoke test, then `worktree_isolation: true` + `parallel: true` on the
opencode entry — so that OpenCode cohorts run concurrently with per-story
isolation.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a fresh git worktree **When** the adapter runs a small edit task
  in it unattended (subprocess cwd = worktree; `--dir` only if cwd proves
  insufficient) **Then** the change lands in that worktree only, and the
  invocation evidence (opencode version, flags, permission config) is recorded.
- **Given** two concurrent adapter tasks in separate worktrees **When** both
  complete **Then** no cross-contamination and no shared-root writes.
- **Given** the evidence **When** the flags flip **Then** the preflight unit
  test shows no PARALLEL_TO_SERIAL degradation for opencode, and a
  verified-negative outcome is documented instead of flipped flags if the
  smoke test fails.

**Technical Notes**: Identical harness-side shape to 29.1-001 — share the
verification script rather than duplicating it (parameterize by adapter).
Watch for OpenCode's per-project state (`.opencode/` or global session store):
confirm concurrent sessions in different directories do not contend on a
shared store; if they do, document the workaround (e.g. per-worktree
`OPENCODE_*` env or config) as part of the evidence.

**Definition of Done**:
- [ ] Shared verification script covers opencode (single + concurrent worktrees)
- [ ] Flags flipped with evidence-citing comment (or verified-negative documented)
- [ ] Preflight unit test: no PARALLEL_TO_SERIAL for opencode
- [ ] Concurrent-session state contention checked and documented

**Dependencies**: 29.2-001 (the adapter and registry entry it verifies)
**Risk Level**: Medium

##### Story 29.2-003: OpenCode usage telemetry from the JSON event stream
**Field finding (2026-09-05, FX)**: the seam is **confirmed and cheap**, no
longer speculative. `opencode run --format json` emits
`step_finish.part.tokens = {total, input, output, reasoning, cache:{write,
read}}` plus `.part.cost`. Verified against both a hosted model and a local
oMLX one. Those are exactly the four keys `evaluate.py`'s `_USAGE_KEYS`
expects, so this is a mapping exercise rather than a discovery exercise.
**User Story**: As FX reading run cost in the dashboard, I want an
`opencode-json` parser that consumes `opencode run --format json` events and
records real token usage on opencode-dispatched stages, so that parallel
OpenCode cohorts stop recording cost as "unavailable".
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an opencode stage run with `--format json` **When** the run ends
  **Then** the parser extracts the RESULT_JSON contract block AND per-session
  token usage from the event stream, and the ledger's stage row records that
  usage (tokens; cost only if the stream provides it — never fabricated).
- **Given** the usage lands **When** capability resolution runs **Then** the
  opencode entry declares `usage_tracking: true` and the degradation plan
  drops `usage_unavailable` for opencode (unit test).
- **Given** a malformed or truncated event stream **When** the parser runs
  **Then** it degrades to the plain-contract path (result block still honored,
  usage recorded unavailable) rather than failing the stage.
- **Given** the claude and codex parsers **When** the suite runs **Then** both
  are untouched — the new parser is additive, selected only by the opencode
  registry entry.

**Technical Notes**: This is the only story in the epic touching parser
Python (`parsers.py` registry). Inspect real `--format json` output first —
event shape is not documented as stable across OpenCode releases, so pin
expectations in fixtures and fail soft. If the stream turns out not to carry
usable token counts, close verified-negative and keep `usage_tracking: false`;
do not scrape approximations.

**Definition of Done**:
- [ ] `opencode-json` parser with fixture-based tests (happy path, truncated
      stream, missing usage)
- [ ] Registry entry switches to `--format json` + new parser;
      `usage_tracking: true`
- [ ] Degradation unit test updated; claude/codex parsers untouched
- [ ] Dashboard/ledger show real usage on an opencode-routed stage

**Dependencies**: 29.2-001 (adapter), 29.2-002 (parallel is the payoff that
justifies telemetry; can technically land independently)
**Risk Level**: Medium

## Epic Sequencing

29.1-001 and 29.2-001 are independent and can run concurrently. 29.2-002
follows 29.2-001; 29.2-003 is the optional tail. Recommended order for a
single serial pass: 29.2-001 → 29.1-001 → 29.2-002 → 29.2-003 — the adapter
story first because it creates the shared verification surface the two
flip stories both exercise.
