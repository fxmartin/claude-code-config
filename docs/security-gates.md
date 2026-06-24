# Security Gates

Coverage is necessary but not sufficient. A 90%-covered diff can still ship a
SQL injection, a hardcoded credential, or a known-vulnerable dependency. Epic-09
embeds security scanning into the same quality gate that measures coverage, so
security becomes a **gate**, not a follow-up.

This page documents the **SAST gate** (Story 9.1-001), the **dependency gate**
(`osv-scanner`, Story 9.1-002), and the **secrets gate** (Story 9.2-001).

## SAST gate (semgrep)

### What it does

After the coverage gate measures coverage, it runs a Static Application Security
Testing scan over the repository and classifies the result into one of three
verdicts:

| Verdict | Meaning | Gate outcome |
|---------|---------|--------------|
| `CLEAN` | No findings at `error` severity (or only `info`) | passes |
| `WARN`  | One or more `warning`-severity findings | passes (advisory) |
| `BLOCK` | One or more `error`-severity findings | **fails**, routes to bugfix loop |
| `SKIPPED` | semgrep not installed, or scan disabled | passes |

A `BLOCK` is treated as a build failure. The orchestrator routes it to the
bugfix loop (Step 5d in `build-stories`), exactly like a dependency
`SECURITY_BLOCK`. The agent output lists each gating finding's rule ID, file,
and line so the bugfix agent can remediate.

### How it runs

The gate uses the `scripts/sast-scan.sh` wrapper, which runs:

```bash
semgrep --config=p/default --config=p/owasp-top-ten --json --output=$REPORT_PATH .
```

`p/default` is semgrep's curated baseline ruleset; `p/owasp-top-ten` adds the
web-application security canon. The wrapper then pipes the JSON report through
the controller's classifier:

```bash
sdlc sast $REPORT_PATH        # prints "SAST_STATUS: CLEAN|WARN|BLOCK", exit 1 on BLOCK
```

Severity mapping (semgrep → verdict):

- `ERROR` → `BLOCK`
- `WARNING` → `WARN`
- `INFO` → ignored (does not gate)

The comparison is case-insensitive.

### Installing semgrep

semgrep is not bundled. Install it one of these ways:

```bash
uv tool install semgrep     # recommended
pipx install semgrep
brew install semgrep
```

If semgrep is absent, the gate reports `SAST_STATUS: SKIPPED` and passes — the
scan is best-effort, never a hard dependency of the build.

### Performance

A 1000-file repo scan with the two default rulesets completes in under 30
seconds on a developer laptop. The `.semgrepignore` file (below) keeps the scan
focused by excluding dependencies, generated code, and test fixtures.

## Dependency gate (osv-scanner)

### What it does

After coverage is measured, the gate checks the project's dependency tree
against the [OSV](https://osv.dev) vulnerability database and classifies the
result into one of three verdicts:

| Verdict | Meaning | Gate outcome |
|---------|---------|--------------|
| `CLEAN` | No known-vulnerable dependencies | passes |
| `WARN`  | One or more low/moderate-severity findings | passes (advisory) |
| `BLOCK` | One or more high/critical-severity findings | **fails**, routes to bugfix loop |
| `SKIPPED` | osv-scanner not installed, or scan disabled | passes |

A `BLOCK` is treated as a build failure and routes to the bugfix loop (Step 5d
in `build-stories`), exactly like a SAST `BLOCK`. The bugfix agent has a
dedicated dependency-remediation path (Step 2b in its prompt): identify the
fixed version, bump the single offending dependency, run the tests, and confirm
the OSV ID is gone. The agent output lists each gating finding's OSV ID,
package, and version so the bump is unambiguous.

### How it runs

The gate uses the `scripts/osv-scan.sh` wrapper, which runs:

```bash
osv-scanner --lockfile=auto --format=json --output=$REPORT_PATH .
```

`--lockfile=auto` auto-detects the lockfiles osv-scanner understands —
`package-lock.json`, `uv.lock`, `poetry.lock`, `go.sum`, `Cargo.lock`, and the
rest. The wrapper then pipes the JSON report through the controller's
classifier:

```bash
sdlc depscan $REPORT_PATH     # prints "DEP_SCAN_STATUS: CLEAN|WARN|BLOCK", exit 1 on BLOCK
```

Severity mapping (OSV → verdict):

- `HIGH` / `CRITICAL` → `BLOCK`
- `LOW` / `MODERATE` (a.k.a. `MEDIUM`) → `WARN`
- a finding with no usable severity label falls back to its CVSS base score
  (≥ 7.0 → `BLOCK`); if that is also absent it is advisory (`WARN`) — a known
  finding never silently passes.

The comparison is case-insensitive.

### Installing osv-scanner

osv-scanner is not bundled. Install it one of these ways:

```bash
brew install osv-scanner                                   # recommended on macOS
go install github.com/google/osv-scanner/cmd/osv-scanner@latest
# or download a release binary from
# https://github.com/google/osv-scanner/releases
```

If osv-scanner is absent, the gate reports `DEP_SCAN_STATUS: SKIPPED` and
passes — the scan is best-effort, never a hard dependency of the build.

### `.dep-scan-suppressions.yaml` — suppress a finding by OSV ID

An optional per-repo file at the repo root. It lists OSV vulnerability IDs to
remove from the gating set, each with **two mandatory fields**:

```yaml
suppress:
  - id: GHSA-9wx4-h78v-vm56
    reason: not reachable — we never set Proxy-Authorization on redirected requests
    expires: 2026-09-30
```

- **`reason`** documents *why* the vulnerability is accepted. A suppression
  without a non-empty reason is a configuration error and fails the gate
  (`exit 2`).
- **`expires`** (ISO `YYYY-MM-DD`) is the review deadline. **CI fails when a
  suppression is past its expiry date** (`exit 2`), so a deferral can never
  silently become permanent — the dependency must be bumped or the deferral
  consciously renewed.

A suppression removes matching findings from the gating set before the verdict
is computed, so a suppressed high-severity CVE no longer blocks. Suppressed
findings are still reported (marked `[suppressed]`) for the audit trail.

### Handling a dependency BLOCK

The default and correct path is to **bump the vulnerable dependency**, not to
suppress it:

1. Read the agent output — each gating finding lists its OSV ID, package, and
   version.
2. Find the lowest non-vulnerable release (osv.dev or the advisory) and bump
   that single dependency in the lockfile.
3. Re-run the gate. It must return `CLEAN` or `WARN` before the story proceeds.
4. Only if no fix exists *and* the finding is genuinely not reachable, add a
   `.dep-scan-suppressions.yaml` entry with a clear `reason` and a near-term
   `expires` date. Suppressions are reviewed in the PR like any other change.

## Secrets gate (gitleaks)

The secrets gate (Story 9.2-001) stops a credential, API key, or token from ever
reaching the default branch. The autonomous build loop commits code on its own;
this gate is the guarantee that it can never autonomously commit a secret. It
runs in two places, both backed by the same [`.gitleaks.toml`](../.gitleaks.toml):

- **CI (mandatory).** The `secrets-scan` job in `.github/workflows/ci.yml` runs
  `gitleaks detect --no-banner --redact` on every pull request and push to
  `main`. It is the **first** job in the pipeline — the build and test jobs
  (`behavior-tests`, `controller-smoke`, `smoke-test`) declare `needs:
  secrets-scan`, so a leaked credential fails the build before any later job
  runs and risks echoing the secret into its logs. Findings are redacted
  (`--redact`): the gate reports *where* a secret is, never the value.
- **Pre-commit (opt-in, recommended).** [`.pre-commit-config.yaml`](../.pre-commit-config.yaml)
  runs the same scan locally before each commit so a leak is caught before it is
  ever pushed. See the install steps in [`onboarding.md`](onboarding.md).

### Configuration

[`.gitleaks.toml`](../.gitleaks.toml) extends the gitleaks default ruleset
(`extend.useDefault = true`) — AWS, GitHub, Slack, Stripe, generic high-entropy,
and more — and adds an allowlist for known-safe patterns:

- **Paths**: `.env.example` / `.env.sample` files and the deliberate test
  fixture `tests/fixtures/leaked-key.txt`. The fixture is allowlisted so its
  planted token never blocks a commit or the CI gate; the bats test
  (`tests/gitleaks-secrets.bats`) scans it with the *default* config instead, so
  detection is still proven.
- **Regexes**: obvious placeholder tokens (`example-key`, `your-token-here`,
  long `xxxx…` runs) so documentation examples that show the *shape* of a
  credential do not trip the gate.

The same config governs CI, the opt-in pre-commit hook, and (on FX's machines)
the Home Manager-managed gitleaks hook — there is one allowlist to maintain, not
three.

### Opt-in: enable the local pre-commit hook

The pre-commit hook is the same scan, run before the secret ever leaves your
machine. It is opt-in because not every contributor uses the
[pre-commit framework](https://pre-commit.com):

```bash
# one-time, per clone
pipx install pre-commit   # or: brew install pre-commit / uv tool install pre-commit
pre-commit install        # registers the git hook from .pre-commit-config.yaml
```

After that, every `git commit` runs `gitleaks protect --staged --redact` against
your staged changes and blocks the commit if it finds a secret.

> FX's machines already run a Home Manager-managed gitleaks pre-commit hook, so
> this step is for colleagues without that setup. Running both is harmless — they
> share `.gitleaks.toml`.

### What to do when the gate finds a real secret

A finding is not a false alarm to be silenced. A committed secret must be treated
as **compromised the moment it lands**, because the history is public the instant
it is pushed. The framework **does not auto-rotate** — that is a deliberate human
decision. Do all of the following, in order:

1. **Rotate the secret first.** Revoke the leaked credential at its source (AWS
   IAM, GitHub token settings, the provider's console) and issue a replacement.
   Removing it from git does **not** un-leak it — assume it was scraped.
2. **Scrub it from history.** Removing the secret in a new commit is not enough;
   it still lives in the earlier commit. Either:
   - rewrite history to drop the secret (`git filter-repo` or BFG) and
     **force-push** the cleaned branch, or
   - if the branch is short-lived, delete and recreate it without the secret.
3. **Re-run the gate.** Push again; `secrets-scan` must come back green before
   the PR can merge.
4. **Add a real false positive to the allowlist — sparingly.** If (and only if)
   the finding is genuinely not a secret (a placeholder, a sample, a hash that
   looks like a key), add a narrow path or regex entry to `.gitleaks.toml` with a
   comment explaining why it is safe. Never allowlist a live value.

## Tuning the scan per repo

### `.semgrepignore` — exclude paths from scanning

Lives at the repo root. Standard gitignore syntax. Use it to exclude code the
team did not write (`node_modules/`, `vendor/`, `.venv/`), generated/minified
files, and test fixtures that are insecure on purpose. The shipped default
already covers these; add repo-specific paths as needed.

`.semgrepignore` is the right tool when you want a **path** out of the scan
entirely. To suppress a **specific finding** while still scanning the file, use
`.sast-config.yaml` instead — it requires a documented reason.

### `.sast-config.yaml` — add rulesets and suppress findings

An optional per-repo file at the repo root. Two keys, both optional:

```yaml
# Append extra semgrep rulesets to the defaults (never removes the defaults).
rulesets:
  - p/python
  - ./rules/custom.yaml

# Suppress findings by semgrep rule ID. Every entry MUST carry a reason.
suppress:
  - id: python.lang.security.audit.formatted-sql-query.formatted-sql-query
    reason: parameterized via the ORM layer; semgrep can't see through it
```

A suppression removes matching findings from the gating set before the verdict
is computed, so a suppressed `error` no longer blocks. Suppressed findings are
still reported (marked `[suppressed]`) for the audit trail.

**The `reason` field is mandatory.** A suppression without a non-empty `reason`
is a configuration error and fails the gate (`exit 2`). This keeps "we turned
off a security check" from ever being silent.

## Handling a BLOCK

When the gate returns `BLOCK`:

1. Read the agent output — each gating finding lists its rule ID, file, and line.
2. Fix the underlying issue (e.g. switch a string-formatted SQL query to a
   parameterized one). This is the default and correct path.
3. Only if the finding is a genuine false positive, add a `.sast-config.yaml`
   suppression with a clear `reason`. Suppressions are reviewed in the PR like
   any other code change.
4. Re-run the gate. It must return `CLEAN` or `WARN` before the story proceeds.

## Verifying the gates locally

```bash
# SAST: scan the working tree (requires semgrep):
bash scripts/sast-scan.sh

# Dependencies: scan the working tree's lockfiles (requires osv-scanner):
bash scripts/osv-scan.sh

# Secrets: scan the working tree exactly as CI does:
gitleaks detect --no-banner --redact --config .gitleaks.toml

# Run the bats coverage for all three gates (requires the tools + bats):
bats tests/sast-scan.bats tests/osv-scan.bats tests/gitleaks-secrets.bats
```

The secrets bats test plants the fixture token in a temp dir, confirms gitleaks
flags it with the default rules, confirms the value is redacted out of the
output, and confirms `.gitleaks.toml` allowlists the fixture at its real path.

## Agent runtime: deny baseline for dispatched agents (Story 13.1-001)

The gates above secure the **target repo's code**. This section secures the
**agent harness itself**. The controller dispatches every agent with
`--dangerously-skip-permissions` (`controller/src/sdlc/dispatch.py`): there is no
human to approve tool calls in a headless `-p` run, so the bypass is what lets the
agent write, commit, and call `gh`. The trade-off is blast radius — without a
floor, a prompt-injected or misbehaving agent could read `~/.ssh`, exfiltrate
secrets, or pipe the internet into a shell.

The deny baseline restores that floor **without** reintroducing prompts (which
would break unattended runs). The permission bypass suppresses the *prompt* but
not an explicit deny list on the command surface: `settings.json`
`permissions.deny` is ignored by the flag, but `--disallowedTools` on the `claude`
invocation is honoured. So `resolve_agent_cmd` appends the baseline as
`--disallowedTools` to the built-in default command. `DENY_BASELINE` blocks, at
minimum:

| Rule | Blocks |
|------|--------|
| `Read(~/.ssh/**)`, `Write(~/.ssh/**)` | SSH key read / tamper |
| `Read(~/.aws/**)` | AWS credential read |
| `Read(**/.env*)` | `.env` secret-file read anywhere in the tree |
| `Bash(curl * \| bash)` | remote "curl \| bash" execution |
| `Bash(ssh *)` | outbound SSH egress |

The rules are narrow by design: they refuse only the listed secret paths and
egress shells, so ordinary edit/test work is unaffected.

**Per-repo override.** Set `SDLC_DENY_BASELINE` to a comma-separated rule list to
replace the baseline for one repo without editing controller code, or to the empty
string to opt out. A `SDLC_AGENT_CMD` / explicit `agent_cmd` is the escape hatch
and owns its own posture — no deny rules are appended to it. The opt-in container
sandbox (Story 13.4-002) is the stronger option for untrusted repos; the deny
baseline is the always-on host-path floor.

## Agent runtime: container sandbox for untrusted repos (Story 13.4-002)

The deny baseline above narrows what an agent can touch on the **host**. For a
repo you don't trust, the **recommended** path is stronger still: give the agent
no host or network reach at all by running it inside a container. It is opt-in —
`sdlc build --sandbox`, or `SDLC_SANDBOX=1` as per-repo config — so trusted local
runs stay on the host.

When enabled, the resolved agent command (deny baseline, routed model, and all)
is **wrapped** in a hardened `<runtime> run` invocation. The wrap is transparent:
the prompt arrives on stdin and the `<<<RESULT_JSON>>>` envelope streams back on
stdout exactly as on the host path, so the branch, commits, usage, and schema
validation are **identical** — the result contract is unchanged. The worktree is
the only bind mount, so the agent's commits land back on the host. The container:

| Flag | Effect |
|------|--------|
| `--network none` | **no egress** by default — neither host nor internet is reachable |
| `--cap-drop ALL` | all Linux capabilities dropped |
| `--security-opt no-new-privileges` | no privilege escalation |
| `--user <uid>:<gid>` | runs as the **non-root** host operator |
| `-v <worktree>:/workspace:Z`, `-w /workspace` | per-story worktree mounted; agent runs there |
| `--rm` | container discarded after the stage |

**Fail-fast.** If `--sandbox` is requested with no container runtime on `PATH`,
dispatch raises `SandboxUnavailableError` **before any agent runs** — it never
silently degrades to an unsandboxed host run. Runtime is auto-detected
(`podman`→`docker`) or forced via `SDLC_SANDBOX_RUNTIME`.

**Knobs.** `SDLC_SANDBOX` (opt-in, covers resumes), `SDLC_SANDBOX_IMAGE`
(the image; must already contain `claude` — the controller never builds it),
`SDLC_SANDBOX_RUNTIME` (force a runtime), `SDLC_SANDBOX_NETWORK` (egress mode;
default `none` — point at a locked-down filtering network only for a stage that
genuinely needs the API). Because egress is off by default, reaching the live API
from inside the sandbox requires opting into such a network — building that proxy
is out of scope; the knob is the hook.

## Untrusted-input sanitization (Story 13.3-001)

The gates above scan the **target repo's code**. This control hardens the
**agent harness itself**. The controller dispatches every agent with
`--dangerously-skip-permissions` (`controller/src/sdlc/dispatch.py`), so the
agent reads its prompt with no permission prompt between that text and the host.
Any untrusted text woven into a prompt — story bodies, issue/PR comments — is
therefore a **prompt-injection surface**: a hidden instruction could hijack a
permission-bypassed agent.

`controller/src/sdlc/sanitize.py` neutralizes that text at the single dispatch
boundary, immediately before the subprocess is launched. It strips or
neutralizes:

| Vector | Category | Action |
|--------|----------|--------|
| Zero-width / bidi Unicode (`U+200B`–`U+200F`, `U+202A`–`U+202E`, `U+2060`–`U+2069`, `U+FEFF`) | `zero-width-unicode` | stripped |
| HTML comments (`<!-- … -->`, multi-line) | `html-comment` | stripped |
| `<script>…</script>` blocks and stray tags | `script-tag` | stripped |
| `data:` URIs (incl. `data:text/html`, `data:…;base64,…`) | `data-uri` | neutralized → `[sanitized:data-uri]` |
| Standalone `base64,` payloads | `base64-payload` | neutralized → `[sanitized:base64]` |

**Conservative code-fence handling.** Inside a fenced code block (```` ``` ````
or `~~~`) only the always-unsafe zero-width/bidi strip applies, so a story that
legitimately *quotes* `<script>` or a `data:` URI as a code sample survives
intact. An *unterminated* fence is treated as prose and fully sanitized — the
safe default. Clean text round-trips byte-for-byte.

**Logging and review routing.** When anything is stripped, the sanitizer emits a
structured `WARNING` on the `sdlc.sanitize` logger naming the `source` stage, the
per-category counts, the weighted `risk_score`, and a `review_recommended` flag.
Each category carries a severity weight (script 5, HTML-comment / data-URI 3,
base64 2, zero-width 1); a `risk_score` at or above `DEFAULT_REVIEW_THRESHOLD`
(3) trips `review_recommended` — so any single script/HTML-comment/data-URI
vector routes the story to human review, while a lone stray zero-width character
is stripped and logged but does not. Clean input logs nothing.

Tests live in `controller/tests/test_sanitize.py` (a malicious-fixture corpus and
a clean corpus, including a code block) and `controller/tests/test_dispatch.py`
(the dispatch-boundary wiring: poisoned prompts are cleaned before the subprocess
sees them; clean prompts pass through unchanged).

## Reference

- SAST wrapper script: `scripts/sast-scan.sh`
- SAST classifier module: `controller/src/sdlc/security_scan.py`
- SAST CLI: `sdlc sast [REPORT_FILE] [--config .sast-config.yaml]`
- Dependency wrapper script: `scripts/osv-scan.sh`
- Dependency classifier module: `controller/src/sdlc/dependency_scan.py`
- Dependency CLI: `sdlc depscan [REPORT_FILE] [--suppressions .dep-scan-suppressions.yaml]`
- Secrets config: `.gitleaks.toml` (CI, pre-commit, Home Manager hook)
- Gate prompt: `plugins/autonomous-sdlc/skills/build-stories/coverage-gate-prompt.md`
- Bugfix prompt (dep remediation): `plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md`
- Bats coverage: `tests/sast-scan.bats` (SAST), `tests/osv-scan.bats` (dependencies), `tests/gitleaks-secrets.bats` (secrets)
- Stories: `docs/stories/epic-09-security-quality-gates.md` (Stories 9.1-001, 9.1-002, 9.2-001)
- Deny baseline: `controller/src/sdlc/dispatch.py` (`DENY_BASELINE`, `resolve_deny_rules`, `SDLC_DENY_BASELINE`); story `docs/stories/epic-13-agent-runtime-security.md` (Story 13.1-001)
- Container sandbox: `controller/src/sdlc/dispatch.py` (`sandbox_enabled`, `detect_container_runtime`, `sandbox_wrap`, `SandboxUnavailableError`, `SDLC_SANDBOX*`); CLI `sdlc build --sandbox`; tests `controller/tests/test_sandbox.py` + `controller/tests/test_dispatch.py`; story `docs/stories/epic-13-agent-runtime-security.md` (Story 13.4-002)
- Input-sanitization module: `controller/src/sdlc/sanitize.py` (Story 13.3-001)
- Input-sanitization tests: `controller/tests/test_sanitize.py`, `controller/tests/test_dispatch.py`
- Harness-hardening story: `docs/stories/epic-13-agent-runtime-security.md` (Story 13.3-001)
