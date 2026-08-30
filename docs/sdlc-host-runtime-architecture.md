# Agentic SDLC Host Runtime on Apple Silicon

**Status:** Architecture — verified against the live host
**Date:** 30 August 2026
**Primary target:** Always-on Mac Studio, with the same design usable on a MacBook Pro (nix profile-gated)
**Primary coding agent:** Claude Code
**Secondary agents:** OpenCode, Pi, Hermes, Codex, or other CLI-based agents as required
**Local inference:** oMLX on macOS
**Remote access:** Claude Code Remote Control plus Blink over Tailscale/Mosh
**Local isolation:** Apple `container` / `container machine`, via `isolated-dev`
**Desktop terminal cockpit:** cmux Free
**Persistent terminal layer:** tmux
**Host configuration:** nix-darwin (`nix-install` repository)
**Repository orchestration:** Existing repo-level Claude Code SDLC framework + external `sdlc` controller

---

## Provenance

This architecture began as a brainstorm on ChatGPT using **Sol 5.6**. The document was
then reworked with **Claude (Fable 5)** in Claude Code, which verified every runtime
assumption against the live host: installed tool versions, the Apple `container` CLI's
actual mount capabilities, the `sdlc` controller's command surface, the `isolated-dev`
project's empirical findings, and the `nix-install` configuration. Where the original
brainstorm asserted, this revision cites what is installed and what was measured.

---

## 1. Executive summary

The recommended architecture is deliberately layered — and most of the layers
**already exist as shipped, versioned assets** on this host. The remaining work is
to connect them, not to build them.

The repository remains the source of truth for the software-development lifecycle.
The existing Claude Code configuration, skills, hooks, specialist agents, quality
gates, worktree rules, and review process continue to define **how software is
developed**. On this host that control plane is not an abstraction: it is the
`claude-code-config` repository plus the external `sdlc` controller
(state machine, ledger, run registry, quality gates, dashboard), and it is
**pinned by the host's own infrastructure-as-code** — `nix-install` carries
`claude-code-config` as a git submodule, so a rebuild deploys a known version of
the control plane.

The Mac provides the execution platform. `cmux` provides the desktop cockpit,
`tmux` provides durable terminal sessions, Apple `container` (wrapped by
`isolated-dev`) provides isolated Linux execution environments, and oMLX provides
local Apple-Silicon inference.

Remote access is split by purpose:

- **Claude Code Remote Control** when the objective is to supervise or interact
  with Claude Code from the Claude iOS application or web.
- **Blink + Tailscale + Mosh** when full terminal access is required: OpenCode,
  oMLX, Git, logs, tests, containers, services, system administration.
- **tmux** ensures the development session survives disconnection from either.

This removes most of the need for a paid cmux Pro subscription and most of the
need for cmux-hosted Cloud VMs.

The target architecture:

```text
                              iPhone / iPad
                         ┌──────────┴──────────┐
                         │                     │
                 Claude Remote Control       Blink
                         │                     │
                         │              Tailscale + Mosh
                         │                     │
                         └──────────┬──────────┘
                                    │
                              Mac Studio
                     (nix-darwin: nix-install repo)
                                    │
                              cmux Free
                                    │
                         persistent tmux sessions
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                  │
             Claude Code         OpenCode          Shell/tools
                 │                  │
                 │                 oMLX
                 │                  │
                 └──────────┬───────┘
                            │
              Repo SDLC framework + sdlc controller
                            │
              ┌─────────────┼─────────────┐
              │             │             │
          architecture   development    review/test
              │             │             │
              └─────────────┼─────────────┘
                            │
              Apple containers (isolated-dev)
                            │
                    Git worktrees / Git
```

The main architectural principle:

> **Do not move SDLC orchestration into the terminal runtime. Keep methodology in
> the repository, execution on the Mac, isolation in Apple containers, persistence
> in tmux, host configuration in nix-darwin, and user interaction in
> cmux/Claude/Blink.**

---

## 2. Starting point — what already exists

The original question was whether a new agentic terminal/runtime such as **Herdr**
could improve a local multi-agent development environment. The answer is shaped by
how much is already built. This is not a greenfield: three of the seven
implementation phases in the original brainstorm turned out to already exist as
shipped projects under other names.

| Layer | Shipped asset | State (verified 30 Aug 2026) |
|---|---|---|
| SDLC control plane | `claude-code-config` repo + `autonomous-sdlc` plugin + external `sdlc` controller | `sdlc` v2.45.12 at `~/.local/bin/sdlc`: `build`, `fix`, `resume`, `status`, `doctor`, `runs`, `dashboard`, `state`, `rollback`, `reconcile`, ledger, SAST/depscan gates |
| Host configuration | `nix-install` (nix-darwin, `~/Documents/nix-install`) | v2.36.0; pins `claude-code-config` as a submodule; manages Tailscale, cmux, oMLX, maintenance and health services; profile-gated (`power` = Mac Studio class) |
| Container isolation | `isolated-dev` (`~/dev/isolated-dev`) | Epic 01 complete (13 stories); Go CLI over Apple `container machine`: `up/open/run/status/stop/upgrade/destroy`, versioned base image, `.isolated-dev.toml` |
| Isolation substrate | Apple `container` CLI | 1.1.0, services running |
| Local inference | oMLX | Installed and pinned by `nix-install/darwin/omlx.nix`; menubar app (see §7) |
| Remote transport | Tailscale, Mosh, Blink | Tailscale via managed cask; `mosh-server` via nix |
| Desktop cockpit | cmux Free | Installed, auto-update pinned by `nix-install` |
| Persistence | tmux | **Not installed** — the one missing foundation |

The remaining problem is therefore **not** "how do we invent an orchestration
framework?" It is:

1. Add the missing persistence layer (tmux, declaratively).
2. Give the `sdlc` controller a host/session layer (`sdlc host …`).
3. Extend `isolated-dev` with a disposable, worktree-scoped worker mode.
4. Decide three open questions (§16): git access from workers, worker
   credentials, and headless inference.

---

## 3. Architectural layers

The design separates five responsibilities.

### 3.1 SDLC control plane

Owned by the repository, pinned by the host IaC.

```text
CLAUDE.md
.claude/  skills/  hooks/  agents/
sdlc controller (state machine, ledger, run registry)
quality gates · worktree policies · Git rules · test rules · security rules
```

Responsibilities: task decomposition, architecture/implementation workflow,
specialist-agent definitions, review/test/security policy, acceptance criteria,
worktree rules, commit/merge policy, auditability.

This remains portable with the repository — and reproducible on the host, because
`nix-install` pins the deployed version via its submodule. A rebuild cannot
silently change which control plane is active.

### 3.2 Agent runtime

Claude Code, OpenCode, Pi, Hermes, Codex. Responsibilities: interpret SDLC
instructions, edit code, invoke tools, call models, run repository workflows.
The SDLC framework should not depend unnecessarily on any terminal multiplexer.

### 3.3 Terminal/session runtime

**cmux Free** as the desktop user interface; **tmux** as the persistent session
substrate. tmux is currently absent from the host and must be added through
`nix-install` (home-manager package plus a declarative `tmux.conf`), not by ad-hoc
install — see §8.

### 3.4 Isolation layer

Apple `container` for disposable workers; Apple `container machine` (via
`isolated-dev`) for longer-lived Linux environments. **These two mechanisms have
opposite filesystem-isolation properties** — the single most important empirical
finding in this document. See §6.

### 3.5 Model/inference layer

Claude Code normally uses Anthropic. OpenCode can use oMLX. oMLX remains native
on macOS to retain Metal acceleration — do **not** move it into a Linux container.

```text
macOS
│
├── oMLX ── local Qwen / Gemma / other model
│
├── Apple VM A ── OpenCode ─────┐
│                               ├── host oMLX endpoint (resolved at startup, §7)
├── Apple VM B ── OpenCode ─────┘
│
└── Claude Code ── Anthropic
```

---

## 4. Remote access

### 4.1 Claude Code Remote Control

The primary remote interface for Claude-specific interaction:

```text
Claude mobile/web → Remote Control → Mac Studio → Claude Code → repo SDLC
```

Use it for: checking Claude progress, answering questions, approving next steps,
sending additional instructions, supervising a long-running session.

Limitation: it controls Claude Code, not the entire Mac development environment.
For anything else, use Blink.

### 4.2 Blink as the universal remote terminal

Use Blink for: shell access, Git, OpenCode, Pi/Hermes, oMLX services, container
management, logs, tests, dev servers, system services, tmux sessions,
infrastructure troubleshooting.

```text
iPhone / iPad → Blink → Tailscale → Mosh → Mac Studio
```

Strategic advantage: **Blink is runtime-independent.** If the desktop runtime
changes later, Blink still works against tmux, a cmux TUI, zellij, plain shells,
container shells, or remote Linux hosts.

### 4.3 Mosh and tmux solve different problems

- **Mosh** provides resilient transport: Wi-Fi→5G handover, changing IPs,
  temporary disconnection, mobile roaming. It is not a persistence layer.
- **tmux** provides durable sessions: when Blink disconnects, the agent keeps
  running inside tmux on the Mac; `mosh macstudio && tmux attach -t <session>`
  re-enters the same terminal.

Recommended combination:

```text
Blink → Mosh → Tailscale → Mac Studio → tmux → Claude / OpenCode / shell
```

### 4.4 Why native cmux alone is not enough

The native cmux application is graphical. Opening it at the desk and later
connecting through Blink does not reproduce the graphical workspace in a terminal.
The model is therefore: **native cmux at the desk, tmux underneath, Blink/Mosh
when remote** — remote users attach to the same underlying tmux session even
though they do not see the cmux GUI. (A cmux headless/TUI session is technically
possible but adds nothing over tmux for this objective.)

---

## 5. Day-to-day workflow

### 5.1 At the Mac Studio

```bash
sdlc host start project-a
```

creates or attaches:

```text
cmux
└── project-a
     └── tmux session: sdlc-project-a
          ├── window: claude     Claude Code
          ├── window: tests      test watcher
          ├── window: logs       application and SDLC logs
          ├── window: shell      normal project shell
          └── window: infra      container/service monitoring
```

Claude Code runs inside the persistent tmux session. cmux displays it comfortably
at the desk; Blink attaches to the same session remotely.

### 5.2 Leaving the desk

Nothing needs to stop. tmux, Claude Code, OpenCode, tests, services, Apple
containers, and oMLX all remain active. cmux can stay open, but persistence never
depends on the GUI.

### 5.3 Quick Claude interaction while away

Claude iOS → Remote Control → Claude Code on the Mac Studio. The simplest route
for Claude-specific decisions.

### 5.4 Full remote control

```bash
mosh macstudio
sdlc host list
sdlc host attach project-a        # or: tmux attach -t sdlc-project-a
```

For infrastructure: `sdlc host status`, `container list`, `sdlc status`,
`sdlc dashboard`.

---

## 6. Isolation design

### 6.1 The mount asymmetry (verified against `container` 1.1.0)

Apple's container stack runs each Linux container inside a lightweight VM, which
is a stronger *kernel* boundary than same-kernel Linux containers. But the two
mechanisms differ radically on the *filesystem* boundary:

| | `container run` | `container machine` |
|---|---|---|
| Mount control | Arbitrary `--volume` / `--mount type=…,source=…,target=…,readonly`, `--tmpfs`, `--read-only` rootfs | **Only** `--home-mount ro\|rw\|none` — the full home directory or nothing; no arbitrary host paths |
| Default | Nothing mounted | **`rw` — full home, read-write** |
| Fine-grained isolation policy | Achievable | Impossible by mounts |

This is not theory. The `isolated-dev` project hit the `container machine`
limitation in production and warns on every `up`:

> `warning: this machine receives read-write access to your full home directory`

It records the active mount scope in project state, surfaces it in `status`, and
locks the full-home fallback to its own versioned base images so a repository
cannot substitute an external image with home access.

Consequence: a **default** `container machine` sees more of the Mac than a
well-scoped Docker container would. The VM boundary does not compensate for a
full-home read-write mount.

### 6.2 Policy: disposable agent workers → `container run`

Autonomous, task-scoped workers use `container run` with explicit mounts:

- Mount only: the assigned worktree (see §6.5 for the git caveat), a cache
  directory, `/tmp`.
- Never mount: `$HOME`, `~/.ssh`, `~/Documents`, sibling worktrees, unrelated
  repositories.
- Lifecycle: `task → create worktree → start container → run worker →
  tests/review → commit or PR → destroy container`.

### 6.3 Policy: long-lived machines → `container machine` via `isolated-dev`

Longer-lived Linux environments (e.g. `sdlc-backend`, `research`,
`integration-test`) use `isolated-dev`, with these rules:

- **`home-mount=ro` is the minimum** for any machine an agent can execute in.
- **`home-mount=rw` is prohibited for autonomous agents.** It is the platform
  default, so the launcher must set the option deliberately on every create.
- `home-mount=none` plus clone-into-guest is the fully-isolated variant for
  aggressive autonomous modes.
- Human-driven use (Zed over SSH, interactive debugging) may accept `ro`/`rw`
  with the standing warning, as `isolated-dev` already implements.

### 6.4 `isolated-dev` as the foundation

`isolated-dev` already provides: idempotent reconcile-toward-declared-state
verbs, a versioned base image with pinned upgrades (`upgrade --yes`, never
automatic), UID/GID-matched guest users, declared-commands-only execution,
secrets by reference (names and paths, never values), port tunnels, and
`--yes`-gated destruction. **Phase 3 extends this tool with a worker mode rather
than building a parallel container layer** (§17).

### 6.5 Git access from workers — open decision (§16)

A git worktree is not self-contained: its `.git` is a pointer file into the main
repository's `.git/worktrees/<name>`, and on this host worktrees live *inside*
the main repo (`.claude/worktrees/…`). Mounting a worktree directory alone breaks
every git command inside the container. Options:

- **(a)** Mount worktree + the main `.git` directory (objects shared, gitdir
  writable). Simplest; leaks sibling refs.
- **(b)** `git clone --local` into the guest, push a branch out on completion.
  Clean isolation; pairs with `home-mount=none`; one clone per task.
- **(c)** Git stays host-side; the container only edits files and the host
  commits. Matches how the `sdlc` fix/build pipeline works today.
- **(d)** Git inside the guest authenticated by SSH-agent forwarding — proven by
  `isolated-dev` for human use; too broad for autonomous agents unless a
  dedicated agent with a scoped key is forwarded instead.

Recommendation: **(c)** for Claude workers (no change to the existing
ledger/commit flow), **(b)** for disposable OpenCode workers.

### 6.6 Worker credentials — open decision (§16)

The isolation policy (no `$HOME`, scoped tokens) collides with putting the Claude
CLI inside a container image: Claude Code authenticates via `~/.claude`.
Options: a dedicated agent-scoped `~/.claude` volume with its own credentials;
API-key environment injection; or **containerize only OpenCode workers** — they
authenticate against local oMLX and need no cloud credentials at all, while
Claude Code stays host-side (consistent with §6.5 option c).

Recommendation: the last. It is the cheapest coherent story and defers nothing
important. The `sdlc` ledger and run registry stay host-side in every variant.

### 6.7 Worktree ownership

Only one layer owns the Git worktree lifecycle:

> **The repository SDLC framework owns worktrees.**

It defines branch naming, worktree paths, creation, merge rules, cleanup, and
conflict policy (`sdlc clean` already garbage-collects orphans). Neither cmux,
nor `isolated-dev`, nor any future runtime creates worktrees unless explicitly
invoked by the SDLC. One source of truth.

---

## 7. Inference layer

### 7.1 Placement

oMLX stays on the macOS host: Apple-Silicon GPU, Metal, unified memory. Workers
in Linux VMs reach it over the network:

```text
Mac Studio
├── oMLX ── local model
├── worker VM A ── OpenCode ──► host oMLX endpoint
└── worker VM B ── OpenCode ──► host oMLX endpoint
```

**Endpoint resolution:** Apple `container` uses vmnet addressing. The Docker
Desktop name `host.container.internal` does not exist here — a copied config
using it fails silently. The launcher must resolve the host gateway address at
startup and inject it into the worker environment; treat any hard-coded endpoint
in configuration as a placeholder.

### 7.2 Daemon vs menubar — open decision (§16)

As deployed by `nix-install/darwin/omlx.nix`, oMLX is a pinned, notarized
**menubar application**. It runs in the user's login session — after a reboot
with no login, there is no inference endpoint, which breaks the "always-on
appliance" goal. Options:

- auto-login (weakens the security model in §13),
- a LaunchAgent starting the app at login, combined with auto-login,
- a **headless serving path** (`mlx_lm.server` is already installed) run as a
  proper nix-darwin launchd service.

Recommendation: the headless service for the always-on Mac Studio role; the
menubar app remains fine for interactive use on the laptop profile.

### 7.3 Concurrency

Local isolation does not create extra compute. Three workers hitting the same
27B model share one GPU/memory subsystem, and mlx-based servers are effectively
single-stream. A declared limit (e.g. `max_concurrent_generations = 1`) is
documentation unless something enforces it. Name the enforcement point — one of:

- verify the oMLX server queues (rather than errors) under concurrent requests
  and let its queue be the semaphore, or
- the launcher holds a per-endpoint lock/semaphore that workers must acquire.

On a 48 GB machine this matters immediately; a future higher-memory Mac Studio
relaxes it but does not remove it.

---

## 8. Host automation (nix-darwin, not hand-written launchd)

The Mac Studio is treated as an always-on agent workstation, and this host is
**nix-darwin managed** (`~/Documents/nix-install`). Hand-written launchd plists
would be fought or orphaned by `darwin-rebuild`; all machine-level services are
declared as nix modules.

Already managed by `nix-install` (verified):

- Tailscale (managed cask)
- oMLX (`darwin/omlx.nix` — pinned notarized DMG, SHA-verified, `power` profile
  only)
- cmux (installed, auto-update pinned)
- maintenance and health services (`maintenance-system.nix`, `health-api.nix`)
- the SDLC control plane itself (`claude-code-config` pinned as a submodule)

To add:

- **tmux** — home-manager package plus declarative `tmux.conf` (the only missing
  foundation)
- the headless inference service, if §7.2 resolves that way
- the `sdlc host` launcher scripts, delivered declaratively (home-manager or
  inside the `sdlc` controller distribution — never loose files in `~/bin`)

The profile system (`profileName == "power"`) already implements "Mac Studio
primary, MacBook Pro usable": runtime-heavy modules are gated to the power
profile.

---

## 9. Launcher: `sdlc host`

An external controller named `sdlc` already exists (v2.45.12) with `status`,
`doctor`, `runs`, `resume`, `dashboard`, `clean`. A parallel `sdlc-*` script
family would collide with it — same verbs, different meanings (run-state vs
host-session-state). The host/session layer is therefore a **subcommand group
extending the existing controller**:

| Command | Effect |
|---|---|
| `sdlc host start <project>` | Resolve repo, read runtime descriptor, check Git state, create/attach tmux session, start required containers, verify inference endpoint if needed, launch the configured agent, start test/log windows, print attach info. Idempotent: reconciles toward declared state. |
| `sdlc host attach <project>` | Attach to the persistent session (wraps `tmux attach -t sdlc-<project>`). |
| `sdlc host status` | Per-project: agent state, inference endpoint, container state, tmux attach state. |
| `sdlc host list` | Known project environments. |
| `sdlc host stop <project>` | Gracefully stop runtime components without deleting repository state. |

`sdlc doctor` already checks install integrity, ledger, config, `gh`, `claude`,
`semgrep`, `osv-scanner`. The host layer **extends** it with Tailscale, Mosh,
tmux, Apple `container`, oMLX, and OpenCode probes — it does not duplicate it.

Verb semantics borrow `isolated-dev`'s culture: every command idempotent and safe
to repeat; destructive operations require `--yes` in the same invocation; never
act on the bare verb.

---

## 10. Runtime descriptor

`isolated-dev` already defines a committed runtime descriptor
(`.isolated-dev.toml`: base image, packages, resources, ports, declared commands,
secrets policy) plus a git-ignored `.isolated-dev.local.toml` for host overrides.
**Do not introduce a second dialect.** The agent-runtime needs extend the same
file:

```toml
version = 1
base_image = "local/isolated-dev-base:1"

[resources]
cpus = 4
memory_gb = 4

[agent]
primary = "claude"            # or "opencode"
isolation = "container-run"   # or "machine", "host"

[inference]
provider = "anthropic"        # or "omlx"
# endpoint is resolved by the launcher at startup (§7.1) — never hard-coded
max_concurrent_generations = 1
```

The descriptor describes **runtime needs only**. It must not duplicate the SDLC
methodology defined in `.claude/` and the controller.

---

## 11. Startup and remote sequences

At Mac boot (all via nix-darwin):

```text
macOS → launchd (nix modules) → Tailscale, inference service, maintenance
```

Starting work:

```bash
sdlc host start project-a
```

Away from home — Claude only: Claude iOS → Remote Control. Full terminal:

```bash
mosh macstudio
sdlc host list
sdlc host attach project-a
```

---

## 12. Failure scenarios

**Mobile connection disappears.** No impact: agents live in tmux on the Mac.

**Wi-Fi changes to cellular.** Mosh handles the transport change gracefully.

**cmux GUI is closed.** Sessions continue; they live in tmux, not the GUI.

**Mac sleeps.** Local execution stops. Prevent it explicitly on the always-on
role: `pmset -a sleep 0 disksleep 0` (or `caffeinate -dimsu` for ad-hoc holds).
This is also why a Mac Studio is a better permanent agent host than a laptop.

**Mac reboots.** nix-darwin restores machine-level services. In-flight runs are
recovered with **`sdlc resume`**, which resumes an interrupted build from the
ledger — no bespoke "project supervisor" is needed. Never auto-recreate
container machines on boot; `isolated-dev` pins machines to their base image and
requires explicit `upgrade --yes`, and that model applies here too. A reboot
cannot restore an in-flight LLM generation; the ledger checkpoint is the
recovery point.

**Agent crashes.** The tmux session persists, the process does not.
`sdlc host status` and the extended doctor make this visible; a supervisor that
restarts failed agents is optional later work.

---

## 13. Security model

The Mac Studio becomes an agent host and is treated accordingly.

**Network** — Tailscale for remote access; no public SSH exposure; firewall
unnecessary services; Mosh only through trusted networking.

**Filesystem** — one worktree per autonomous worker; explicit scoped mounts via
`container run` (§6.2); `home-mount=rw` never for autonomous agents (§6.3); no
broad home-directory access.

**Credentials** — scoped GitHub credentials; separate personal and agent
credentials where practical; no SSH keys inside workers (or a dedicated scoped
agent only, §6.5d); API secrets only where required; secrets by reference, never
inline (enforced by the descriptor, §10).

**Agent permissions** — no unrestricted host execution. Aggressive autonomous
modes run inside Apple containers with `home-mount=none` or scoped `container
run` mounts.

**Logging** — SDLC decisions, commits, test outcomes, review results, agent
artifacts, and quality-gate evidence stay in the repository and the `sdlc`
ledger/run registry (host-side, §6.6).

---

## 14. Options considered and deferred

The brainstorm evaluated several products at length; the conclusions survive in
the decision table (§15). In brief:

**Herdr** — an agent-aware runtime with programmatic agent lifecycle APIs
(`agent.start/prompt/wait/read`). Deferred: the repo SDLC already owns
orchestration, and a second control plane weakens determinism, auditability, and
worktree ownership. It becomes interesting only if the SDLC is later generalized
into a vendor-neutral agent execution framework (§17, Phase 7).

**cmux Pro / cmux iOS** — polished mobile workspace UI, push notifications,
browser-pane streaming, Cloud VM allowance. Deferred: Claude Remote Control and
Blink already cover the two real remote-access needs; Pro is convenience, to be
re-evaluated on actual friction rather than purchased upfront.

**cmux Cloud VMs** — remote isolated Linux workers. Overflow option only: they
win on independence from the Mac (work continues when it is off), lose on
recurring cost, source code leaving the Mac, and the absence of local
Apple-Silicon inference. Apple containers provide equivalent isolation locally
at no hourly cost.

**CodeRouter** — provider/account routing, failover, quota management. Deferred:
the model path is already simple (Claude Code → Anthropic, OpenCode → oMLX);
revisit only if it gains strong support for arbitrary OpenAI-compatible and
local endpoints with latency- and task-aware selection.

---

## 15. Decisions and rationale

| Decision | Selected approach | Reason |
|---|---|---|
| SDLC orchestration | Repo framework + `sdlc` controller | Already implemented, auditable, pinned by nix-install |
| Agent runtime | Claude Code primarily | Existing SDLC integration |
| Local alternative agent | OpenCode | Direct local-model integration; no cloud credentials needed in workers |
| Desktop cockpit | cmux Free | Strong UX without subscription |
| Persistent sessions | tmux (add via nix-install) | Universal, reliable, easy remote reattach |
| Mobile Claude control | Claude Remote Control | Best Claude-specific UX |
| General mobile terminal | Blink | Already owned, runtime-independent |
| Remote transport | Tailscale + Mosh | Private networking + resilient roaming |
| Disposable worker isolation | `container run`, scoped mounts | Only mechanism supporting fine-grained mounts (verified, §6.1) |
| Long-lived Linux env | `container machine` via `isolated-dev` | Shipped tooling; `home-mount=ro`/`none` for agents |
| Local inference | oMLX on macOS | Metal/GPU performance; never in a Linux VM |
| Worktree ownership | Repo SDLC | One source of truth; `sdlc clean` exists |
| Host startup | nix-darwin modules (`nix-install`) | Host is nix-managed; hand-written plists would be orphaned |
| Project launcher | `sdlc host` subcommands | Extends the existing controller; avoids a colliding `sdlc-*` family |
| Runtime descriptor | `.isolated-dev.toml` + `[agent]`/`[inference]` | One dialect; tool already parses and validates it |
| Container layer | Extend `isolated-dev` | Image versioning, idempotency, `--yes` safety already built and tested |
| Herdr | Defer | Second control plane not needed |
| cmux Pro / iOS | Defer | Existing tools cover the use cases |
| cmux Cloud VM | Optional overflow | Only when independence from the Mac is required |
| CodeRouter | Defer | Limited value with current routing needs |

---

## 16. Open decisions

Three questions are deliberately left open, each with a marked recommendation:

1. **Git access from containerized workers** (§6.5). Recommended: host-side git
   for Claude workers (option c), clone-into-guest for disposable OpenCode
   workers (option b).
2. **Worker credentials** (§6.6). Recommended: containerize only OpenCode
   workers; Claude Code stays host-side until Phase 7 makes a dedicated
   agent-credential volume worthwhile.
3. **Headless inference** (§7.2). Recommended: `mlx_lm.server` (or equivalent)
   as a nix-darwin launchd service on the always-on profile; menubar oMLX for
   interactive laptop use. Includes verifying queue-vs-error behavior under
   concurrent requests (§7.3).

---

## 17. Implementation phases

Phases are mapped to the assets that already exist; three of the original seven
are complete or nearly so.

**Phase 1 — Persistent local sessions.** Add tmux via `nix-install`
(home-manager + declarative config). Implement `sdlc host start/attach/list/
status/stop`. *Goal: a project launched at the desk can be resumed from Blink
without losing the agent session.*

**Phase 2 — Claude Remote Control.** Enable and validate. *Goal: Claude-specific
supervision from iPhone/iPad without a terminal.*

**Phase 3 — Worker isolation (extend `isolated-dev`).** Already shipped for
long-lived machines. Add a worker mode — `isolated-dev worker <worktree>` —
wrapping `container run` with scoped mounts, no Zed/SSH/ports, reusing the
versioned base image and reconciliation machinery. Resolve open decisions 1–2.
*Goal: autonomous workers execute in isolated Linux VMs seeing only their
worktree.*

**Phase 4 — Runtime descriptor extension.** Add `[agent]`/`[inference]` to
`.isolated-dev.toml` (§10); `sdlc host start` reads it. *Goal: any repository
launches without project-specific shell scripts.*

**Phase 5 — Host automation completion.** Mostly done in `nix-install`. Remaining:
resolve open decision 3 (headless inference service), sleep settings (`pmset`),
deliver launcher scripts declaratively. *Goal: the Mac Studio behaves as an
always-on development appliance surviving reboot without manual steps.*

**Phase 6 — Multi-worker scheduling.** Worker limits, the inference concurrency
enforcement point (§7.3), CPU/RAM quotas, container lifecycle policies. *Goal:
parallel agents do not overwhelm unified memory or the inference endpoint.*

**Phase 7 — Optional generic runtime adapter.** Only if later required: abstract
agent execution behind `start/prompt/wait/read/stop`, targeting Claude,
OpenCode, Herdr, or a cloud worker. *Goal: the repo SDLC becomes
agent-runtime-independent without changing methodology.*

---

## 18. Target operating model and final recommendation

```text
                         ALWAYS-ON MAC STUDIO
                     macOS (nix-darwin) + Tailscale
                                  │
                              cmux Free
                                  │
                               tmux
                                  │
             ┌────────────────────┼────────────────────┐
             │                    │                    │
         Project A            Project B            Project C
          Claude              OpenCode               Claude
             │                    │                    │
       Apple VM A            Apple VM B            Apple VM C
       worktree A            worktree B            worktree C
                                  │
                                 oMLX ── local large model

              iPhone / iPad:  Claude app → Remote Control
                              Blink → Tailscale + Mosh
```

The recommendation is not to buy another orchestration layer. It is to **compose
a small number of tools with clearly separated responsibilities** — most of which
are already built:

```text
Repo SDLC + sdlc     decides the process and records it (ledger)
Claude/OpenCode      perform the work
isolated-dev         isolates the work (Apple containers)
oMLX                 serves local models (native, Metal)
tmux                 preserves sessions
cmux Free            presents the desktop cockpit
Claude Remote        controls Claude remotely
Blink + Mosh         controls everything remotely
Tailscale            provides private connectivity
nix-install          keeps the host declarative and pinned
```

The most important design decision is unchanged: **the repository remains the
control plane** — and on this host that is enforced by construction, because the
IaC repository pins the control plane's version. Everything else is
infrastructure. This preserves portability, keeps orchestration deterministic,
minimizes recurring cost, and leaves a clean upgrade path toward a generic
multi-agent runtime if the SDLC ever needs to orchestrate Claude, OpenCode,
Codex, local models, and cloud workers through one common API.
