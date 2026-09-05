<!-- ABOUTME: "Add a new harness" onboarding guide + generic CLI adapter template walkthrough (Story 20.6-001). -->
<!-- ABOUTME: Shows that wiring a new agent CLI is a config + wrapper change, never a Python change. -->

# Adding a new harness

The controller dispatches each pipeline role (build, coverage/qa, review, merge,
docs) to a **harness** — an agent CLI wrapped so it speaks one neutral contract.
Adding a harness is a **config + wrapper-script change, no Python edits**: you
declare an entry in [`controller/src/sdlc/config/harnesses.yaml`](../controller/src/sdlc/config/harnesses.yaml),
point it at a wrapper script, reuse an existing output parser, and declare what
the harness can do. The controller code never changes.

This guide walks the five moving parts, then a complete worked example for a
hypothetical harness. The shipped **codex** and **qwen** entries are the
canonical real examples; **opencode**, **pi**, and **gemini** are the candidate
future targets this abstraction exists for.

## The contract every harness speaks

A harness is just a command the controller runs once per agent dispatch:

1. **The prompt arrives on the wrapper's stdin.** The controller assembles the
   role prompt (story body, repo context, sanitized inputs) and pipes it to the
   harness command — there is no prompt CLI flag to thread through.
2. **The harness runs headless** — no TTY, no interactive approval prompts.
3. **The final answer carries a result block.** The agent ends its output with

   ```text
   <<<RESULT_JSON>>>
   { ...the role's response JSON... }
   <<<END_RESULT>>>
   ```

   The controller scans stdout for this block and validates it against the role
   schema in [`docs/contracts.md`](contracts.md); prose around the block is
   ignored. A non-zero exit is a dispatch failure.

That is the whole boundary. Anything that can read a prompt on stdin and end its
output with a `<<<RESULT_JSON>>>` block is a candidate harness.

## The five moving parts

### 1. A wrapper script

Copy the generic template at
[`controller/adapters/generic-cli-adapter.sh`](../controller/adapters/generic-cli-adapter.sh)
to `controller/adapters/<harness>-adapter.sh` and set its one `AGENT_CMD` line to
your CLI (e.g. `codex exec`, `opencode run`). The template already:

- reads the prompt on stdin and hands it to your CLI on *its* stdin,
- forwards the CLI's stdout verbatim, so a result block the CLI emits round-trips
  untouched, and
- fails fast (exit 64) with an actionable message if no CLI is wired.

Prove the round-trip before wiring anything else:

```bash
controller/adapters/generic-cli-adapter.sh --self-test
```

`--self-test` emits a schema-valid `build` result block with no real CLI — the
controller's contract parser accepts it unedited. If your CLI cannot be coaxed
into emitting the result block directly, your wrapper is the place to translate
its native output into one (still no Python).

### 2. A `harnesses.yaml` entry

Add a key under `harnesses:` whose `command:` invokes your wrapper. The command
template may use `{pr_number}`, `{pr_url}`, and `{story_id}` placeholders.

### 3. A parser declaration

Each entry names a `parser:` — the registered interpreter for that harness's
stdout. Reuse an existing id; do **not** add a parser unless your harness has
genuinely new telemetry semantics:

| Parser id           | Use it when                                                                 |
| ------------------- | --------------------------------------------------------------------------- |
| `claude-stream-json`| The harness is Claude (stream-json envelope, usage, rate-limit, overflow).   |
| `codex-exec`        | A plain CLI with a JSON contract but **no** usage/rate-limit telemetry. This is the parser any new stdin→`<<<RESULT_JSON>>>` harness should declare, including Qwen Code's `qwen -p` wrapper. |

The `codex-exec` parser reads the result block straight from stdout, records
usage as *unavailable* (rather than a misleading zero), and treats every
non-zero exit as a plain dispatch failure. That is exactly the generic template's
behaviour, so a copied wrapper pairs with `codex-exec` out of the box.

### 4. Capability flags

Declare what the harness can do. Undeclared canonical flags default to **false**
(a harness only earns a capability it explicitly claims), so a non-Claude CLI
declares the few it supports and the controller degrades the rest safely — e.g. a
harness without `worktree_isolation`/`parallel` is run serially instead of
crashing a parallel cohort mid-run.

| Capability           | Meaning                                                            |
| -------------------- | ------------------------------------------------------------------ |
| `worktree_isolation` | Can run each agent in its own git worktree.                        |
| `parallel`           | Can fan a cohort across concurrent workers.                        |
| `json_contract`      | Emits the `<<<RESULT_JSON>>>` block.                               |
| `usage_tracking`     | Reports token usage / cost. Declaring it `false` (or omitting it) makes every token and cost figure for this harness **unavailable** — `—`, never `0` — in the ledger, scoreboard and dashboard, and makes the token/cost axes *not comparable* in `sdlc eval-compare` (31.2-002). Flip it to `true` when the adapter grows real usage telemetry; nothing else has to change. |
| `rate_limit_aware`   | Surfaces a recoverable, time-based rate-limit signal.             |

Optionally add a `probe:` command (a cheap "is the CLI installed/authenticated?"
check). A zero exit means available; a non-zero exit degrades to a warning in
preflight rather than a mid-run crash. Omit it to skip the check.

### 5. Cost metering (optional, Story 31.2-003)

`metered:` and `local_rate_usd_per_million_tokens:` are plain top-level
declarations, not `capabilities:` flags — they state an economic fact about
the harness rather than something the controller gates a run mode on:

| Field                              | Meaning                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------ |
| `metered`                           | Whether this harness's cost is a real/notional $/Mtok figure. Defaults to `true` — every harness that omits it keeps today's hosted-API assumption. |
| `local_rate_usd_per_million_tokens` | An explicit, recorded $/Mtok-equivalent rate (e.g. an assumed energy cost). Only consulted when `metered` is `false`; ignored otherwise. |

Set `metered: false` for a harness with no per-token price at all — local
inference (e.g. an oMLX-backed CLI) being the motivating case: it still
reports real token counts (`usage_tracking: true` is unaffected), but its own
`cost` telemetry, even a literal `cost: 0`, is never a real dollar figure and
must not be trusted at face value. With no `local_rate_usd_per_million_tokens`
configured, `sdlc eval`'s scoreboard renders that harness's cost as **"not
metered"** (never `0`, never a `$/Mtok` extrapolation), and `sdlc eval-compare`
against a metered harness renders the cost row as a labelled non-comparison —
never a delta implying the local arm saved a specific dollar amount. Configure
`local_rate_usd_per_million_tokens` to price it anyway under an explicit,
recorded assumption; that rate travels with every number it produces via the
scoreboard's `provenance` block. See
[`docs/evaluation.md`](evaluation.md#cost-for-local-inference-31-2-003) for the
full behaviour.

Epic-14/28's build-time cost governance (budget gates, calibration) never
reads these fields — a build stage is always priced via the hosted convention
regardless of which harness ran it, so a not-metered harness cannot affect a
budget gate; only `sdlc eval`/`eval-compare` (Epic-31) consume them.

### 6. Per-stage model routing (optional)

A registry harness can route a **different model per pipeline stage** — the
OpenAI analog of Epic-14's Claude Balanced map (build on a capable model, the
mechanical merge/coverage on a cheaper one, the adversarial skeptic on a stronger
one). Epic-14's `haiku`/`sonnet`/`opus` aliases are Claude-only, so a non-Claude
harness carries **its own model ids**.

The **shipped default does not opt in**: the `codex` entry's `command` has no
`{model}` placeholder, so Codex uses whatever model your `~/.codex/config.toml`
declares (e.g. `gpt-5.5`). That keeps a build runnable on any authenticated Codex
without assuming a model entitlement — **use model ids you actually have**, since a
model your account can't serve fails the whole stage with a 400 (e.g. ChatGPT-account
Codex rejects `gpt-5.4-codex`; verify any id with `echo hi | codex exec --model <id>`).

Opt in with two pieces: a `{model}` placeholder in `command`, and a `models:` map
of stage → model id. The controller substitutes the stage's mapped model into the
placeholder at dispatch; your wrapper forwards it to the CLI (the codex wrapper
forwards `--model <id>` to `codex exec`).

```yaml
codex:
  command: "codex-build-adapter.sh --model {model}"
  parser: codex-exec
  models:                  # use ids your account is entitled to (these are examples)
    default: gpt-5.5       # required when command uses {model}
    build: gpt-5.5
    coverage: gpt-5.5      # point at a cheaper model for mechanical stages if you have one
    review: gpt-5.5
    merge: gpt-5.5
    adversarial: gpt-5.5   # point at a stronger skeptic if you have one
```

Rules:

- A command using `{model}` **must** declare a `default` — it covers any stage not
  listed (e.g. the `bugfix`/`reask` recovery agents), so an unmapped stage always
  resolves rather than failing. The registry loader rejects a `{model}` command
  with no `default`.
- A harness whose command has **no** `{model}` placeholder (the shipped default)
  routes a single fixed model — whatever the CLI defaults to — so it never assumes
  an entitlement. No map needed, no behaviour change.
- The Claude harness is unaffected: its per-stage Haiku/Sonnet/Opus routing
  (Epic-14) flows through the dispatch seam exactly as before.
- The model is chosen by the **stage** (build, coverage, review, merge,
  adversarial, …), the same stage the ledger records — so a heterogeneous run is
  auditable down to which model ran each stage.

## Worked example: a hypothetical `acme` harness

Suppose Acme ships a headless CLI, `acme run`, that reads a prompt on stdin and
prints a `<<<RESULT_JSON>>>` block. Wiring it is three steps and **no Python**.

**Step 1 — wrapper.** Copy the template and set the CLI:

```bash
cp controller/adapters/generic-cli-adapter.sh controller/adapters/acme-adapter.sh
# edit acme-adapter.sh: AGENT_CMD="acme run --headless"
controller/adapters/acme-adapter.sh --self-test   # confirms the contract round-trips
```

**Step 2 — registry entry.** Add a key under `harnesses:` in
`controller/src/sdlc/config/harnesses.yaml` (the existing `default: claude` and the
`claude`/`codex` entries stay as they are):

```yaml
harnesses:
  acme:
    command: "controller/adapters/acme-adapter.sh"
    parser: codex-exec
    enabled: true
    probe: "acme --version"
    capabilities:
      worktree_isolation: false
      parallel: false
      json_contract: true
      usage_tracking: false
      rate_limit_aware: false
```

**Step 3 — route a role.** Point any pipeline role at it on the build command:

```bash
sdlc build-stories --harness review=acme,qa=acme
```

The controller resolves `acme` from the registry, runs the wrapper with the
prompt on stdin, parses the result block with `codex-exec`, and — because `acme`
declares neither `parallel` nor `worktree_isolation` — automatically runs that
role serially with a logged warning instead of failing. No `sdlc/*.py` file was
touched.

## Running a Codex-worker build

Routing a role to `codex` (`sdlc build --harness build=codex,…`, Story 20.7-001)
dispatches that stage's worker through
[`scripts/codex-build-adapter.sh`](../scripts/codex-build-adapter.sh). The harness
registry invokes that adapter (and the qwen and opencode ones) by **bare name**,
resolved on PATH at dispatch. `install.sh --core` installs them automatically: it
symlinks `scripts/codex-build-adapter.sh`, `scripts/qwen-build-adapter.sh`, and
`scripts/opencode-build-adapter.sh` into
`~/.local/bin` (the same dir `uv` installs `sdlc` into), so a PATH-installed
controller runs a cross-harness build with no manual step. If you have **not** run
the installer, link them by hand as a fallback:

```bash
ln -sf "$PWD/scripts/codex-build-adapter.sh"    ~/.local/bin/
ln -sf "$PWD/scripts/qwen-build-adapter.sh"     ~/.local/bin/
ln -sf "$PWD/scripts/opencode-build-adapter.sh" ~/.local/bin/
```

Getting a codex-worker run green on a host then comes down to three things — get
them wrong and you hit an auth or sandbox dead-end instead of a clear error:

1. **Pre-authenticate codex first.** The controller runs the worker **headless**
   (no TTY, no interactive approval), so it cannot complete a login flow mid-run.
   Run `codex login` (or set the API key the CLI expects) once on the host and
   confirm `codex exec` works non-interactively *before* starting a build.
2. **Grant non-interactive write/exec via `HARNESS_AGENT_CMD`.** A worker has to
   edit files and run commands without stopping for per-action approval. Modern
   Codex uses `--sandbox workspace-write` for that (the older `--full-auto` is
   **deprecated** — it warns and maps to the same thing). The adapter honours
   `HARNESS_AGENT_CMD`, so export it to override the default `codex exec`. But note
   `workspace-write` also **blocks network**, which the worker's `gh` push/PR calls
   need (see point 3) — so for a trusted repo the practical override is the
   full-access mode:

   ```bash
   # trusted repo (worker may write AND reach the network for gh):
   export HARNESS_AGENT_CMD="codex exec --dangerously-bypass-approvals-and-sandbox"
   # …or workspace-write + codex `network_access = true` in ~/.codex/config.toml:
   # export HARNESS_AGENT_CMD="codex exec --sandbox workspace-write"
   sdlc build epic-20 --harness build=codex,coverage=codex
   ```
3. **Do not combine a Codex worker with the controller `--sandbox` flag, and run
   on the host path.** The controller's `--sandbox` is **Claude-only** — it runs
   the agent inside a **no-egress** container image that has neither the Codex CLI
   nor network. A Codex worker must run on the **host path** instead: its `gh`
   operations (branch push, PR open, status checks) need the **network** and
   GitHub auth that *both* the controller's no-egress image **and** Codex's own
   `workspace-write` sandbox block. Leave `--sandbox` off and grant the worker
   network either with `--dangerously-bypass-approvals-and-sandbox` (point 2) or by
   enabling Codex's own `network_access`.

> **Provenance.** Per-role `--harness` routing was a ledger **label** only until
> Story 20.7-001: `cli.py` validated the resolved harnesses and then discarded
> them, so `--harness build=codex` *labelled* the ledger while every stage still
> ran `claude`. Story 20.7-001 wired the routing through the build loop, so a
> codex-routed stage now dispatches the Codex adapter for real.

## Setting a repo's default harness

Passing `--harness …` on every `sdlc build` gets old in a repo that always wants
the same routing. Drop a `.sdlc-harness.yaml` at the **consumer repo root** to
declare a default harness (and, optionally, a per-role map) once — mirroring the
sibling `.sdlc-model-routing.yaml` and `.sdlc-risk-config.yaml` override files
(Story 20.7-005). A sample:

```yaml
# .sdlc-harness.yaml — per-repo harness routing for `sdlc build`.
harness:
  # Every pipeline role (build, coverage, review, merge, docs) routes here unless
  # overridden below. Omit `default:` to keep the built-in `claude` default and
  # only remap specific roles.
  default: codex
  roles:
    # Per-role overrides win over `default:`. Role names match `--harness`
    # (build / coverage / review / merge / docs; `qa` aliases `coverage`).
    review: claude
    qa: codex
```

**Precedence** is `--harness` flag **>** repo file **>** built-in `claude`
default:

- With no file and no flag, behaviour is unchanged — every role runs on `claude`.
- The file's `default:` routes every role it does not name in `roles:`.
- An explicit `--harness` flag always wins over the file, role by role.

The file is validated in the same preflight as the flag: a malformed file, an
unknown role, or a `default:`/`roles:` harness that is unknown or disabled in
[`controller/src/sdlc/config/harnesses.yaml`](../controller/src/sdlc/config/harnesses.yaml) fails
fast (exit 2) before any stage runs — no half-run.

## Running an eval on another harness

`sdlc eval` (Story 31.1-001) is the other consumer of this registry, alongside
`sdlc build`/`sdlc fix`. Its config gains a `harness:` field, and `--harness`
on the CLI wins over it — the same CLI > config > default precedence as the
eval's `model:` pin:

```bash
# The config's harness: field (default: claude if absent).
uv run sdlc eval

# --harness overrides it, so the same fixed ticket set is comparable across
# harnesses by construction — the scoreboard records which one ran.
uv run sdlc eval --harness qwen --json
```

Resolution and preflight happen once, before any ticket dispatches: an
unknown or disabled harness, a failed `probe`, or a harness whose command has
no `{model}` placeholder (so it cannot honour the eval's model pin) all abort
before a single ticket runs — never mid-run. See
[`docs/evaluation.md`](evaluation.md#harness-selection-31-1-001) for the full
config format and abort matrix.

## Shipped non-Claude adapters

Beyond `codex` (the worked reference throughout this page), the registry ships:

- **qwen** — Qwen Code headless coding agent; `qwen-build-adapter.sh` using `qwen -p`.
- **opencode** — open-source headless coding CLI; `opencode-build-adapter.sh`
  using `opencode run --pure` with the prompt on stdin (`run` reads its message
  from stdin when given no positional; argv delivery would cap the prompt at
  Linux's 128 KiB `MAX_ARG_STRLEN`). Two things must be true before routing a
  role here, and **both fail as an indefinite hang rather than an error**:
  - the target repo's `opencode.json` must set
    `"permission": { "edit": "allow", "bash": "allow" }`, or OpenCode blocks on
    an interactive approval prompt with no TTY to answer it;
  - OpenCode's own resolved default model must be reachable and authenticated
    (the registry entry pins no model, per issue #228). Check it with
    `echo ping | opencode run --pure`; if it hangs, set
    `OPENCODE_FLAGS='--model <provider/model>'`, which the adapter forwards and
    which survives the `uv tool install --force` that rewrites `harnesses.yaml`.

  Both matter because this adapter runs on the captured dispatch path, where the
  300s output-idle stall detector does not apply — a hang costs the full 3600s
  wall-clock timeout, per stage.

## Candidate future targets

The abstraction exists so these become config exercises, not engineering
projects:

- **pi** — lightweight agent CLI; stdin prompt, JSON result.
- **gemini** — Google's CLI; wrap `gemini`'s headless mode to emit the result block.

Each is the same recipe: a wrapper that maps stdin→CLI and CLI-stdout→result
block, a `harnesses.yaml` entry with `parser: codex-exec`, and honest capability
flags. The [codex, qwen, and opencode entries](../controller/src/sdlc/config/harnesses.yaml) are the
canonical real-world examples to copy from.

## Where the boundary stays Claude-only

The controller-driven `build-stories` and `fix-issue` paths above are both
controller-backed (issue #436 migrated `fix-issue` into `sdlc fix`). `fix-issue`
stays Claude-only **by policy** — it exposes no `--harness` flag yet — while
`resume-build-agents` stays Claude-only **by mechanism**: it uses Claude Code's
in-process `Agent` tool (`subagent_type`, `isolation="worktree"`), which has no
CLI equivalent. See [`docs/controller-architecture.md`](controller-architecture.md)
for the controller module map.
