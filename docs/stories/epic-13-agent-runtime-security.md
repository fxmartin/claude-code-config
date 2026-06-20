# Epic 13: Agent Runtime Security Hardening

> **Status: PLANNED** — created 2026-06-20. Inspired by the threat model in the external
> [affaan-m/ECC](https://github.com/affaan-m/ECC) `the-security-guide.md`. Distinct from
> Epic-09 (which scans the *target code* via SAST/OSV/gitleaks): this epic hardens the
> **agent harness itself** — the controller that dispatches headless agents with
> `--dangerously-skip-permissions` and the framework FX distributes to five colleagues.

## Epic Overview

**Epic ID**: Epic-13
**Description**: The controller dispatches every agent as `claude -p --output-format json
--dangerously-skip-permissions` (`controller/src/sdlc/dispatch.py:34`), so a dispatched agent
runs with **no permission prompts** — full read/write/shell on the host. `settings.json`
carries a `permissions.allow` list and `defaultMode: auto` but **no `deny` list**, and nothing
sanitizes untrusted text (story bodies, issue/PR comments) before it reaches an agent, nor scans
installed hooks/skills/MCP configs as the supply-chain artifacts they are. A misbehaving or
prompt-injected agent today has the blast radius of the whole machine, and a runaway agent has no
kill-switch beyond the preflight timeout. This epic adds the missing runtime guardrails:
credential deny-rules, supply-chain scanning, untrusted-content sanitization, and a
kill-switch/heartbeat — with an optional container sandbox for untrusted repos.

**Business Value**: FX runs long unattended autonomous batches and ships this framework to five
LTM colleagues. Autonomy with a permission bypass is only safe if the *isolation layer* keeps up
with the *convenience layer*: a single injected instruction or a poisoned hook must not be able
to read `~/.ssh`, exfiltrate secrets, or run forever. Hardening makes "fire-and-forget overnight"
defensible and makes the framework safe to hand to colleagues.

**Success Metrics**:
- A dispatched agent **cannot read** `~/.ssh`, `~/.aws`, or `**/.env*`, nor run `curl … | bash`
  / `ssh …`, by default — verified by a test that asserts the deny baseline is in effect.
- Installing or updating the framework **scans** every hook/skill/MCP/settings artifact for
  dangerous patterns and **fails CI** on a match, so a poisoned config never runs unreviewed.
- Untrusted text reaching an agent is **sanitized** (zero-width Unicode, HTML comment/script,
  `data:` URIs stripped/flagged), proven against a malicious-fixture corpus.
- A stalled or runaway agent is **killed within a bounded heartbeat window** (process group, not
  just the parent), and its logs are quarantined for review — no run hangs indefinitely.

## Epic Scope

**Total Stories**: 5 | **Total Points**: 19 | **MVP Stories**: 0 (roadmap — 13.1-001 is Must Have)

## Out of Scope (Non-Goals)

- **Abandoning autonomy / removing `--dangerously-skip-permissions`.** The deny baseline narrows
  blast radius; it does not reintroduce interactive prompts that would break unattended runs.
- **Securing the *target* repo's code.** SAST/OSV/gitleaks (Epic-09) own that. This epic secures
  the *harness*.
- **A full multi-tenant identity system.** Scoped-credential guidance is documentation; building
  an account-provisioning service is not in scope.
- **Mandatory sandboxing for every run.** The container sandbox (13.4-002) is opt-in for
  untrusted repos; trusted local runs stay on the host.

## Features in This Epic

### Feature 13.1: Permission & Credential Boundaries

Narrow what a dispatched agent can touch, without breaking unattended autonomy.

#### Stories

##### Story 13.1-001: Deny-rules baseline for agent dispatch
**User Story**: As FX running agents with permissions bypassed, I want a default deny baseline for
secret-bearing paths and dangerous shell so that an injected or misbehaving agent cannot read my
credentials or pipe the internet into a shell, even though prompts are suppressed.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the controller dispatches an agent **When** the agent command is constructed
  **Then** a deny baseline is applied — at minimum `Read(~/.ssh/**)`, `Read(~/.aws/**)`,
  `Read(**/.env*)`, `Write(~/.ssh/**)`, `Bash(curl * | bash)`, `Bash(ssh *)` — so these are
  refused even under `--dangerously-skip-permissions`.
- **Given** the deny baseline **When** a legitimate agent does normal work (edit repo files, run
  the test command) **Then** it is unaffected — the baseline blocks only the listed
  secret/egress paths, not ordinary development.
- **Given** a repo that needs an exception **When** the operator sets a documented override
  **Then** the baseline can be relaxed per-repo without editing controller code.

**Technical Notes**: `--dangerously-skip-permissions` bypasses the prompt, so the deny list must
be enforced via the dispatch command surface (e.g. a `--disallowedTools`/deny configuration on
the `claude` invocation, or a constrained `SDLC_AGENT_CMD`) rather than `settings.json`
`permissions.deny` (which the bypass ignores). Touches `controller/src/sdlc/dispatch.py` (the
`["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions", …]` builder).
Document the trade-off in `docs/controller-architecture.md` and the security reference.

**Definition of Done**:
- [ ] Deny baseline applied to every dispatched agent under the permission bypass
- [ ] Legitimate edit/test work unaffected (regression test)
- [ ] Per-repo documented override
- [ ] Test asserts the secret-path/egress denials are in effect
- [ ] Documented in `docs/controller-architecture.md` + security reference

**Dependencies**: None
**Risk Level**: Medium

### Feature 13.2: Supply-Chain Trust

Treat hooks, skills, MCP servers, and settings as supply-chain artifacts that are scanned before
they run.

#### Stories

##### Story 13.2-001: Scan hooks/skills/MCP/settings for dangerous patterns
**User Story**: As FX distributing this framework, I want every installed hook, skill, MCP
config, and settings file scanned for dangerous patterns so that a poisoned or accidentally
unsafe artifact is caught before it ever executes.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the repo's hooks/skills/MCP/settings **When** a scan runs (a `sdlc` check and a CI
  job) **Then** it flags dangerous patterns — `curl|wget|nc|scp|ssh`, `enableAllProjectMcpServers`,
  `ANTHROPIC_BASE_URL` overrides, `base64,`/`data:text/html`, and zero-width Unicode — reporting
  file, line, and pattern.
- **Given** a match in CI **When** the job runs on a PR **Then** the job fails, so a poisoned
  config cannot merge unreviewed.
- **Given** a legitimate use of a flagged token **When** it is reviewed **Then** an allowlist
  entry suppresses that specific finding (no blanket disabling of the scan).

**Technical Notes**: Reuse the `rg`-based pattern approach from ECC's guide. Implement as a
controller check (classify CLEAN/WARN/BLOCK like `sast`/`depscan` in `security_scan.py` /
`dependency_scan.py`) plus a CI job alongside the existing Epic-02 validators. Scan
`hooks/`, `skills/`, `plugins/**/skills/`, `mcp/config.template.json`, and `settings.json`.

**Definition of Done**:
- [ ] Pattern scanner over hooks/skills/MCP/settings with CLEAN/WARN/BLOCK verdicts
- [ ] CI job fails the PR on a BLOCK
- [ ] Per-finding allowlist (no blanket disable)
- [ ] Tests with poisoned + clean fixtures
- [ ] Documented in the security reference

**Dependencies**: None
**Risk Level**: Medium

### Feature 13.3: Untrusted-Content Sanitization

Sanitize external text before a privileged agent ever reads it.

#### Stories

##### Story 13.3-001: Sanitize untrusted inputs before agent dispatch
**User Story**: As FX, I want story text, issue bodies, and PR comments sanitized before they are
embedded in an agent prompt so that hidden instructions cannot hijack a permission-bypassed agent.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** untrusted text destined for an agent prompt (story description, issue/PR body)
  **When** it is prepared for dispatch **Then** zero-width/bidi Unicode
  (`​-‍⁠﻿‪-‮`), HTML comments/`<script>`, and `data:`/`base64,`
  payloads are stripped or escaped, and the action is logged.
- **Given** a suspicious payload is detected **When** sanitization runs **Then** the controller
  records an event (and, above a threshold, can route the story to human review rather than
  silently proceeding).
- **Given** ordinary, clean text **When** sanitized **Then** it passes through unchanged (no
  corruption of legitimate code blocks or markdown).

**Technical Notes**: Apply at the dispatch boundary in `controller/src/sdlc/dispatch.py` /
`discovery.py` where story text is assembled into the agent prompt. Keep a structured log of what
was stripped. Conservative escaping for code fences so real backticks/snippets survive.

**Definition of Done**:
- [ ] Sanitizer strips/escapes zero-width Unicode, HTML comment/script, data/base64 payloads
- [ ] Suspicious-payload detection emits a ledger event; over-threshold can gate to review
- [ ] Clean text round-trips unchanged (incl. code blocks)
- [ ] Tests against a malicious-fixture corpus + clean corpus
- [ ] Documented in the security reference

**Dependencies**: None
**Risk Level**: Medium

### Feature 13.4: Loss-of-Control Safeguards

Guarantee a runaway or stalled agent can always be stopped — and, optionally, never had host
access to begin with.

#### Stories

##### Story 13.4-001: Kill-switch and heartbeat dead-man for dispatched agents
**User Story**: As FX running unattended batches, I want a stalled or runaway agent to be killed
automatically within a bounded window so that a hung or looping agent never holds the run (or the
machine) hostage.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a dispatched agent subprocess **When** it must be terminated (timeout, abort, stall)
  **Then** the controller kills the **process group** (`kill(-pid, SIGKILL)`), not just the
  parent, so orphaned children cannot survive.
- **Given** a running agent **When** it stops emitting output/heartbeat for longer than a bounded
  interval **Then** a supervisor treats it as stalled and terminates it with a clear message,
  rather than waiting for the full preflight timeout.
- **Given** an agent is killed **When** the run continues or exits **Then** the killed agent's
  transcript/logs are quarantined for review and the event is recorded in the ledger.
- **Given** graceful vs hard shutdown **When** terminating **Then** the controller attempts
  `SIGTERM` first and escalates to `SIGKILL` after a grace period.

**Technical Notes**: Touches `controller/src/sdlc/dispatch.py` (subprocess launch — use a new
process group / `start_new_session=True` so the group can be signalled) and the build loop's
timeout handling. Complements Epic-12 story 12.1-002 (preflight hang guard) — that prevents
recursive self-invocation; this bounds *any* stall. Heartbeat can ride Epic-11 streaming
(11.1-001) when present, else fall back to an output-idle timer.

**Definition of Done**:
- [ ] Process-group kill (TERM→KILL escalation) on timeout/abort/stall
- [ ] Output-idle heartbeat supervisor with a bounded stall window
- [ ] Killed-agent logs quarantined; event recorded in the ledger
- [ ] Tests: a hanging synthetic agent is killed within the window; children do not survive
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: None (pairs with Epic-12 12.1-002; heartbeat pairs with Epic-11 11.1-001)
**Risk Level**: High

##### Story 13.4-002: Optional container sandbox mode for untrusted repos
**User Story**: As FX reviewing or building in an untrusted repo, I want to run dispatched agents
inside a no-egress container so that even a fully compromised agent cannot reach my host or the
network.
**Priority**: Could Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `--sandbox` (or per-repo config) **When** the controller dispatches an agent
  **Then** the agent runs inside a container with the workspace mounted, **no network egress**
  (`network=none`/internal), all Linux capabilities dropped (`cap_drop: ALL`,
  `no-new-privileges`), and a non-root user.
- **Given** sandbox mode **When** a stage completes **Then** results (the `<<<RESULT_JSON>>>`
  envelope, branch, commits) are produced exactly as on the host path — the contract is unchanged.
- **Given** no container runtime is available **When** `--sandbox` is requested **Then** the
  controller fails fast with a clear message rather than silently running unsandboxed.

**Technical Notes**: Use the existing Podman/OCI competency (`podman-container-architect`,
`docs/container-best-practices.md`). Mount the worktree, run `claude` inside the container,
stream the envelope back. Egress-off by default; explicit allowlist only if a stage needs it.
Likely the largest lift in this epic — may be deferred.

**Definition of Done**:
- [ ] `--sandbox` dispatch in a no-egress, cap-dropped, non-root container
- [ ] Result contract identical to the host path (verified by test)
- [ ] Fail-fast when no container runtime present
- [ ] Documented as the recommended path for untrusted repos

**Dependencies**: 13.1-001 (deny baseline is the host-path floor; sandbox is the stronger option)
**Risk Level**: High

## Story Dependencies (within Epic-13)

```
13.1-001 (deny baseline) ──> 13.4-002 (sandbox, optional)
13.2-001 (supply-chain scan)   independent
13.3-001 (input sanitization)  independent
13.4-001 (kill-switch)         independent (pairs with Epic-12 12.1-002, Epic-11 11.1-001)
```

- **Cohort 1** (no deps): 13.1-001, 13.2-001, 13.3-001, 13.4-001
- **Cohort 2**: 13.4-002 (needs 13.1-001)

## Epic Complete When

- A dispatched agent cannot read secret paths or pipe the internet into a shell by default, even
  under the permission bypass.
- Hooks/skills/MCP/settings are scanned on install and in CI; a poisoned artifact fails the gate.
- Untrusted text is sanitized before any agent sees it, proven against a malicious corpus.
- A stalled/runaway agent is killed within a bounded heartbeat window (process group), with logs
  quarantined — no run hangs indefinitely.
- The optional container sandbox runs untrusted repos with no host/network reach while leaving the
  result contract unchanged.
