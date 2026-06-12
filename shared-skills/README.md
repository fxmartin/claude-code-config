<!-- ABOUTME: Index of the shared skill set — the single source of truth synced to the Codex mirror. -->
<!-- ABOUTME: Hosted in claude-code-config; consumed by nix-install via git submodule (ADR-002). -->

# Shared skills (source of truth)

This directory is the **single source of truth** for the skills shared between
the Claude harness (`claude-code-config`, this repo) and the Codex mirror
(`nix-install`). It exists so these skills cannot drift between the two
runtimes — there is exactly one copy.

The decision and rationale are recorded in
[`docs/adr/002-codex-mirror-sync.md`](../docs/adr/002-codex-mirror-sync.md):
`claude-code-config` is the source of truth, and `nix-install` consumes this
directory as a git submodule.

## Skills

| Skill | Purpose |
|-------|---------|
| `check-releases` | Monitor upstream dependency updates and assess risk. |
| `coverage` | Test coverage analysis and gap identification. |
| `create-issue` | Author precise, actionable GitHub issues. |
| `create-project-summary-stats` | Generate project retrospectives from repo history. |
| `plan-release-update` | Plan Nix-based release updates. |
| `project-review` | Senior-lead architecture and quality review. |
| `roast` | Opinionated senior code review. |

## Syncing (consumer side)

A consumer repo pulls the latest shared skills with one command:

```bash
git submodule update --remote
```

After updating, verify byte-for-byte parity with the source:

```bash
sdlc sync-check <source>/shared-skills <consumer>/shared-skills
```

## Releasing (source side)

The shared skill set is versioned by the repo's release tags. The release
workflow records changes under `shared-skills/` in the CHANGELOG, so each
`vX.Y.Z` tag is a stable, pinnable artifact a consumer can lock onto. See
`scripts/sync-shared-skills.sh --help`.
