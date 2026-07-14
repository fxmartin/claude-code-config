# High-Risk File Approval Gate

The high-risk gate is a human-in-the-loop checkpoint (Epic-08, Story 8.2-001).
Any pull request that touches a high-risk path — auth, payments, migrations,
infrastructure, secrets, or destructive shell — is blocked at merge until a
human approves it, regardless of how green the rest of the pipeline is. The gate
is the safety valve that lets the rest of the autonomous loop run aggressively
without risking an unintended `DROP TABLE users` at 3 am.

Approval is satisfied by **either** of two paths:

- an approving review from a member of the `risk-approver` GitHub team (the path
  for organisation repos); or
- the `risk-approved` label added by a maintainer with write access (the path
  for single-maintainer / personal repos, where there is no org team to check
  and a PR author cannot approve their own PR).

On **GitLab** (Free/Core) there is no `risk-approver` team-review path, so the
`risk-approved` **label** is the sole maintainer-approval signal — the Free/Core
equivalent of the GitHub gate's label path (Story 23.5-001). Both gate labels
(`risk:high` and `risk-approved`) are provisioned by `sdlc issues init` on
either host, so the labels always exist for a maintainer to apply. The
adversarial reviewers feeding the gate source the MR diff host-aware via
`glab mr diff` (see [adversarial-review.md](adversarial-review.md)).

**The gate is not enforced on GitLab today.** Detection lives in
`.github/workflows/risk-gate.yml` — a GitHub Actions workflow — and the shipped
`.gitlab-ci.yml` template carries no risk-gate job, so nothing detects high-risk
paths or applies `risk:high` to an MR on its own. Even a maintainer-applied
`risk:high` label fails no pipeline job and triggers no approval rule, so
nothing platform-side blocks `glab mr merge`; the only backstop is the merge
agent's prompt instruction to refuse and park the story `AWAITING_APPROVAL` —
best-effort prompt compliance, not an enforced control. Porting the gate
(detector + a blocking pipeline job) to the GitLab CI template is an open item.

## How it works

1. **Detection.** `.github/workflows/risk-gate.yml` runs on every pull request.
   It diffs the PR against its base and pipes the changed files through
   `scripts/risk-gate-detect.sh`, which matches each path against the glob
   patterns in `controller/src/sdlc/config/high-risk-patterns.yaml`.
2. **Flagging.** If any file matches, the workflow:
   - adds the `risk:high` label to the PR;
   - posts (and keeps updated) a comment listing the matched files, the pattern
     each one hit, and the two approval paths; and
   - **fails the `risk-gate` check** until the PR is approved by one of those
     paths.
3. **Approval.** Either a `risk-approver` team member approves the PR, or a
   maintainer (write+ access) adds the `risk-approved` label. The workflow
   re-runs on the review or label event and the check turns green. The label
   path verifies the labeller's repo permission, so a drive-by contributor
   cannot self-approve by applying the label.
4. **Merge.** The merge agent (`build-stories` merge prompt) refuses to merge a
   `risk:high` PR that has no human approval and **never** uses
   `gh pr merge --admin` to bypass the gate.

## Configuration

The baseline patterns live in `controller/src/sdlc/config/high-risk-patterns.yaml` as a
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
