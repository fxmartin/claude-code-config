# Security Gates

Coverage is necessary but not sufficient. A 90%-covered diff can still ship a
SQL injection, a hardcoded credential, or a known-vulnerable dependency. Epic-09
embeds security scanning into the same quality gate that measures coverage, so
security becomes a **gate**, not a follow-up.

This page documents the **SAST gate** (Story 9.1-001). Dependency scanning
(`osv-scanner`, Story 9.1-002) and secrets scanning (`gitleaks`, Story 9.2-001)
are documented here as they land.

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

## Reference

- Wrapper script: `scripts/sast-scan.sh`
- Classifier module: `controller/src/sdlc/security_scan.py`
- CLI: `sdlc sast [REPORT_FILE] [--config .sast-config.yaml]`
- Gate prompt: `plugins/autonomous-sdlc/skills/build-stories/coverage-gate-prompt.md`
- Bats coverage: `tests/sast-scan.bats` (verifies `tests/fixtures/sql-injection.py` → `BLOCK`)
- Story: `docs/stories/epic-09-security-quality-gates.md` (Story 9.1-001)
