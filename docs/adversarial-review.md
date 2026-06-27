# Adversarial Review Slot

The adversarial review slot is a vendor-agnostic interface that lets any second
reviewer — Codex, GPT-5, Gemini, or a deterministic SAST tool — review a PR
before merge. The orchestrator never names a specific reviewer; it dispatches
through the slot and acts on a structured verdict. Swapping the reviewer runtime
(Codex today, something else tomorrow) is a config change, not a code change.

Introduced in Story 8.1-001 (Epic-08).

## Interface contract

### Input

The controller hands every reviewer the same request shape:

```json
{
  "pr_number": 42,
  "pr_url": "https://github.com/fxmartin/repo/pull/42",
  "story_id": "8.1-001",
  "diff": "diff --git ...",
  "context": {
    "tests_pass": true,
    "coverage_pct": 93.5,
    "review_approved": true
  }
}
```

In Python this is the frozen dataclass `ReviewRequest` (with a nested
`ReviewContext`) in `controller/src/sdlc/adversarial.py`.

### Output

Every reviewer must emit this shape (JSON-schema draft 2020-12, published at
`controller/src/sdlc/schemas/adversarial-reviewer-response.schema.json`):

```json
{
  "reviewer_name": "codex",
  "verdict": "approve",
  "summary": "One-paragraph human-readable summary.",
  "findings": [
    {
      "severity": "warn",
      "category": "security",
      "file": "src/auth.py",
      "line": 42,
      "message": "Token expiry not validated."
    }
  ]
}
```

- `verdict` is one of `approve`, `request_changes`, `block`.
- `findings[].severity` is one of `info`, `warn`, `error`, `critical`.
- `findings[].line` may be `null` for a file-level finding.
- Extra top-level fields are allowed (forward-compat); the required set is
  `reviewer_name`, `verdict`, `summary`, `findings`.

`parse_reviewer_response()` validates the output against the schema and raises
`AdversarialContractError` with the offending field named when it does not
conform.

## Config: `controller/config/adversarial-reviewers.yaml`

Reviewers are registered in YAML. Each entry declares the command to invoke, a
timeout, an enabled flag, and the verdicts it is allowed to return. A top-level
`consensus` key selects the rule applied across reviewers.

```yaml
consensus: any_block_majority

reviewers:
  codex:
    command: "codex review-pr --pr-number {pr_number} --output json"
    timeout_sec: 300
    enabled: true
    allowed_verdicts: ["approve", "request_changes", "block"]
  gemini:
    command: "gemini-review --pr {pr_url} --format json"
    timeout_sec: 300
    enabled: false
    allowed_verdicts: ["approve", "request_changes", "block"]
```

Command templates may use `{pr_number}`, `{pr_url}`, and `{story_id}`
placeholders; the controller substitutes them before invoking the reviewer.

### Link to the harness registry (Story 20.3-002)

Codex appears in two registries: here, as the `codex` *reviewer*, and in
`controller/config/harnesses.yaml`, as the `codex` *harness* the `review`/`qa`
roles route to (`sdlc build --harness review=codex,qa=codex`). To keep those
from becoming **two competing Codex configurations**, the reviewer registry is a
**view** over the harness registry:

```yaml
reviewers:
  codex:
    harness: codex          # <- links to the `codex` entry in harnesses.yaml
    command: "codex-adversarial-review.sh --pr-number {pr_number}"
    timeout_sec: 300
    enabled: true
    allowed_verdicts: ["approve", "request_changes", "block"]
```

- **Single source of availability.** The linked harness (`harnesses.yaml`) owns
  whether Codex is available — its `enabled` flag and optional `probe`. The
  reviewer entry owns only the review-role specifics Epic-08's consensus needs:
  the review command, `timeout_sec`, `allowed_verdicts`, and the file-level
  `consensus` rule. The two commands are *intentionally* different — `codex exec`
  is the build/QA invocation, `codex-adversarial-review.sh` is the PR-review
  invocation — but there is now exactly one switch for "is Codex set up".
- **Preflight reconciliation.** When a run passes a `--harness` map, `sdlc build`
  runs `reconcile_reviewer_registry` in preflight and **fails fast** (no
  half-run) if the link has diverged: a reviewer that links a harness absent from
  `harnesses.yaml` (a dangling link), or an **enabled** reviewer linked to a
  **disabled** harness (the harness registry's availability switch must win).
- **Consensus is untouched.** The link is identity-only. `dispatch_adversarial_review`
  still invokes every enabled reviewer in parallel and applies the same consensus
  rule — Epic-08 owns that semantics and Story 20.3-002 does not change it.

A reviewer with no `harness:` key (e.g. `gemini` above) stays standalone: it is
not reconciled against the harness registry and carries its own availability.

### Enabling and disabling reviewers

Flip `enabled` to add or remove a reviewer from the gate. To swap Codex for a
stub that always approves, point a reviewer's `command` at the stub and disable
the rest — no orchestrator code changes. Users without a given reviewer
installed simply leave it `enabled: false`.

## Codex reference implementation

The first concrete plug-in is the Codex reviewer (Story 8.1-002). It ships as a
wrapper script, `scripts/codex-adversarial-review.sh`, registered as the `codex`
command in `adversarial-reviewers.yaml`. The same script is distributed with the
Codex `autonomous-sdlc` plugin (`nix-install`) so it is on `PATH` wherever Codex
runs.

```
codex-adversarial-review.sh --pr-number <N> [--reviewer-skill roast|project-review]
```

What it does:

1. Fetches the PR diff with `gh pr diff <N>`.
2. Runs a Codex review skill via `codex exec` — `roast` by default, or
   `project-review` (choose per repo with `--reviewer-skill`, or set
   `CODEX_ADV_REVIEW_SKILL`). The skill is instructed to end its output with a
   single fenced ` ```json ` block in the slot's response shape.
3. Extracts that block, forces `reviewer_name` to `codex`, records which skill
   ran in an extra `reviewer_skill` field (the schema allows extra fields), and
   prints the normalised JSON to stdout.

The output validates against
`controller/src/sdlc/schemas/adversarial-reviewer-response.schema.json`, so the
controller's `parse_reviewer_response()` accepts it unchanged. If the transcript
contains no parseable JSON or an out-of-range verdict, the wrapper exits
non-zero and prints nothing — it fails closed rather than waving a PR through.

The wrapper never shells out during tests: setting `CODEX_ADV_RAW_OUTPUT` to a
captured transcript file makes it parse that instead of calling `gh`/`codex`,
which is how the `tests/codex-adversarial-review.bats` suite and the controller
`test_codex_adversarial_review.py` schema-validity test run hermetically in CI.

### Disabling the Codex reviewer

`codex` is `enabled: true` by default. If you do not have Codex installed,
disable it in **`harnesses.yaml`** (`harnesses.codex.enabled: false`) — that is
the single availability switch, and the linked reviewer follows it. Disabling
only the reviewer while leaving the harness enabled is fine; the reverse
(reviewer enabled, harness disabled) is the divergence the preflight rejects, so
turn the harness off and set the reviewer to match:

```yaml
reviewers:
  codex:
    harness: codex
    command: "codex-adversarial-review.sh --pr-number {pr_number}"
    timeout_sec: 300
    enabled: false   # <- match the harness when Codex is not available
    allowed_verdicts: ["approve", "request_changes", "block"]
```

With every reviewer disabled the slot is inert and `/build-stories` behaves as it
did before the gate existed — the Codex reviewer is mandatory only when it is
configured and enabled, so users without Codex see no behavior change.

## Consensus rules

The active rule lives in the config (`consensus:`), not in code, so changing it
does not require a release.

- **`any_block_majority`** (default): any `block` verdict blocks the merge.
  Otherwise the majority verdict wins; ties resolve toward `request_changes`
  (the cautious choice). If no reviewer produced a verdict, the result fails
  safe with `block`.
- **`unanimous_approve`**: every enabled reviewer must `approve`, else the
  result is `block`.

With two LLM reviewers, three verdicts are possible and they can disagree. The
default rule is a starting point; pick `unanimous_approve` for stricter repos.

## Dispatch

```python
from sdlc.adversarial import ReviewContext, dispatch_adversarial_review

result = dispatch_adversarial_review(
    pr_number=42,
    story_id="8.1-001",
    diff=diff_text,
    context=ReviewContext(tests_pass=True, coverage_pct=93.5, review_approved=True),
    pr_url="https://github.com/fxmartin/repo/pull/42",
    config_path="controller/config/adversarial-reviewers.yaml",
)

print(result.consensus)        # "approve" | "request_changes" | "block"
print(result.consensus_rule)   # which rule produced it
for v in result.verdicts:      # per-reviewer detail
    print(v.reviewer_name, v.verdict, v.summary)
```

`dispatch_adversarial_review` reads the config, invokes every **enabled**
reviewer in parallel (one thread each), validates each response against the
schema, collects the verdicts, and applies the consensus rule. The `invoke`
keyword argument is the dispatch seam: production shells out to the reviewer
command; tests pass a fake returning canned output so no real reviewer runs in
CI.
