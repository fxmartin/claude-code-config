<!-- ABOUTME: "Add a new harness" onboarding guide + generic CLI adapter template walkthrough (Story 20.6-001). -->
<!-- ABOUTME: Shows that wiring a new agent CLI is a config + wrapper change, never a Python change. -->

# Adding a new harness

The controller dispatches each pipeline role (build, coverage/qa, review, merge,
docs) to a **harness** — an agent CLI wrapped so it speaks one neutral contract.
Adding a harness is a **config + wrapper-script change, no Python edits**: you
declare an entry in [`controller/config/harnesses.yaml`](../controller/config/harnesses.yaml),
point it at a wrapper script, reuse an existing output parser, and declare what
the harness can do. The controller code never changes.

This guide walks the four moving parts, then a complete worked example for a
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

## The four moving parts

### 1. A wrapper script

Copy the generic template at
[`controller/adapters/generic-cli-adapter.sh`](../controller/adapters/generic-cli-adapter.sh)
to `controller/adapters/<harness>-adapter.sh` and set its one `AGENT_CMD` line to
your CLI (e.g. `codex exec`, `opencode run --quiet`). The template already:

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
| `usage_tracking`     | Reports token usage / cost.                                        |
| `rate_limit_aware`   | Surfaces a recoverable, time-based rate-limit signal.             |

Optionally add a `probe:` command (a cheap "is the CLI installed/authenticated?"
check). A zero exit means available; a non-zero exit degrades to a warning in
preflight rather than a mid-run crash. Omit it to skip the check.

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
`controller/config/harnesses.yaml` (the existing `default: claude` and the
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

## Candidate future targets

The abstraction exists so these become config exercises, not engineering
projects:

- **qwen** — Qwen Code headless coding agent; shipped as `qwen-build-adapter.sh` using `qwen -p`.
- **opencode** — open-source headless coding CLI; `opencode run`-style invocation.
- **pi** — lightweight agent CLI; stdin prompt, JSON result.
- **gemini** — Google's CLI; wrap `gemini`'s headless mode to emit the result block.

Each is the same recipe: a wrapper that maps stdin→CLI and CLI-stdout→result
block, a `harnesses.yaml` entry with `parser: codex-exec`, and honest capability
flags. The [codex and qwen entries](../controller/config/harnesses.yaml) are the
canonical real-world examples to copy from.

## Where the boundary stays Claude-only

The controller-driven `build-stories` path above is cross-harness. The in-process
`fix-issue` / `resume-build-agents` skills are **not** — they use Claude Code's
in-process `Agent` tool (`subagent_type`, `isolation="worktree"`), which has no
CLI equivalent. See [`docs/controller-architecture.md`](controller-architecture.md)
for the controller module map.
