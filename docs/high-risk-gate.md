# High-Risk File Approval Gate

The high-risk gate is a human-in-the-loop checkpoint (Epic-08, Story 8.2-001).
Any pull request that touches a high-risk path — auth, payments, migrations,
infrastructure, secrets, or destructive shell — is blocked at merge until a
human on the `risk-approver` GitHub team approves it, regardless of how green
the rest of the pipeline is. The gate is the safety valve that lets the rest of
the autonomous loop run aggressively without risking an unintended
`DROP TABLE users` at 3 am.

## How it works

1. **Detection.** `.github/workflows/risk-gate.yml` runs on every pull request.
   It diffs the PR against its base and pipes the changed files through
   `scripts/risk-gate-detect.sh`, which matches each path against the glob
   patterns in `controller/config/high-risk-patterns.yaml`.
2. **Flagging.** If any file matches, the workflow:
   - adds the `risk:high` label to the PR;
   - posts (and keeps updated) a comment listing the matched files, the pattern
     each one hit, and the required reviewers; and
   - **fails the `risk-gate` check** until a `risk-approver` team member submits
     an approving review.
3. **Approval.** When a `risk-approver` member approves, the workflow re-runs on
   the review event and the check turns green.
4. **Merge.** The merge agent (`build-stories` merge prompt) refuses to merge a
   `risk:high` PR that has no human approval and **never** uses
   `gh pr merge --admin` to bypass the gate.

## Configuration

The baseline patterns live in `controller/config/high-risk-patterns.yaml` as a
flat YAML list under `high_risk_patterns:`. Glob semantics:

- `**` crosses path separators (a leading `**/` also matches at the repo root);
- `*` matches within a single path segment (never crosses `/`);
- `?` matches a single non-separator character.

Baseline patterns:

```yaml
high_risk_patterns:
  - "**/auth/**"
  - "**/authentication/**"
  - "**/payments/**"
  - "**/billing/**"
  - "**/migrations/**"
  - "**/.github/workflows/**"
  - "Dockerfile*"
  - "**/*.tf"
  - "**/*.tfvars"
  - "**/secrets/**"
  - "**/*.sh"  # destructive shell scripts; can be narrowed
  - "**/iam/**"
  - "**/policies/**"
```

A pattern like `**/*.sh` is deliberately broad. The point is to fail-safe, not
to be perfectly precise — real-world narrowing happens after first contact with
traffic.

### Per-repo overrides

A consumer repo can add its own patterns by committing a
`.sdlc-risk-config.yaml` file at its root with the same `high_risk_patterns:`
shape. Overrides are **additive**: listed patterns are appended to the baseline
(de-duplicated), never replacing it. Both the workflow detector and the
controller's `sdlc.risk_gate` module honor the override.

```yaml
# .sdlc-risk-config.yaml — additive, repo-local
high_risk_patterns:
  - "**/terraform/state/**"
  - "config/production/**"
```

## The `risk-approver` team

Human approval is gated on membership of the `risk-approver` GitHub team in the
repository's organization. For a solo user, the team has one member (FX); for
the LTM pilot it stays solo. Post-pilot, team members can be added — the team
list is the approver roster.

## Controller integration

`controller/src/sdlc/risk_gate.py` exposes the same detection logic to the
controller side:

- `load_patterns(config_path=..., override_path=...)` — read the baseline plus
  any additive per-repo override.
- `matches_pattern(path, pattern)` — single-glob match with the semantics above.
- `match_high_risk(changed_files, ...)` — map each high-risk changed file to the
  first pattern it hit; an empty result means the change set is clean.

## Testing

- `tests/risk-gate.bats` verifies pattern detection against fixtures of file
  paths (`tests/fixtures/risk-gate/`), including the additive override path.
- `controller/tests/test_risk_gate.py` covers the glob matcher, config loading,
  and override merge for the Python module.
