<!-- ABOUTME: ADR-002 recording the Codex mirror sync mechanism for the shared skill set. -->
<!-- ABOUTME: Decision: claude-code-config is source of truth; nix-install consumes via submodule. -->

# ADR-002: Codex Mirror Sync Mechanism

- **Status**: Accepted (extended by [ADR-003](003-harness-neutral-skill-format.md))
- **Date**: 2026-06-12
- **Epic / Story**: Epic-07 / Story 7.4-001
- **Deciders**: FX

## Context

The `autonomous-sdlc` harness ships as two mirror plugins: one for Claude Code
(this repo, `claude-code-config`) and one for the Codex runtime (the sibling
`nix-install` repo). A handful of skills are shared verbatim between the two
runtimes — `check-releases`, `coverage`, `create-issue`,
`create-project-summary-stats`, `plan-release-update`, `project-review`, and
`roast`. Until now these lived as duplicated copies in both repos, which means
they can — and did — drift. A reliable cross-runtime harness needs exactly one
copy of each shared skill.

Two candidate mechanisms were considered:

1. **New third repo** `fxmartin/sdlc-shared-skills` as the source of truth, with
   *both* `claude-code-config` and `nix-install` consuming it as a git submodule.
2. **`claude-code-config` is the source of truth**, and `nix-install` pulls the
   shared skills via a git submodule pointed at this repo.

## Decision

**`claude-code-config` is the source of truth for the shared skill set; the
`nix-install` Codex mirror consumes it as a git submodule.** (Option b.)

The shared skills live in `shared-skills/` in this repo. Each skill is a single
`*.md` file; there is exactly one copy. A consumer repo fetches updates with one
command:

```bash
git submodule update --remote
```

The shared skill set is versioned by this repo's existing release tags (Epic-05),
so a consumer can pin to a stable `vX.Y.Z` artifact rather than tracking a moving
branch. Byte-for-byte parity between source and consumer is verified by the
controller's tested `sdlc sync-check` command, wrapped by
`scripts/sync-shared-skills.sh`.

## Rationale

- **No new repo to provision, secure, or release.** `claude-code-config` already
  has CI, a release workflow that produces versioned tags (Epic-05), and is the
  primary builder in the harness. A third repo would add a release surface and a
  second set of branch protections for no functional gain.
- **The shared skills already originate here.** They were authored as Claude
  slash-commands in this repo; making this repo the source of truth keeps edits
  where they happen and removes the duplicate copies.
- **Single submodule edge.** `nix-install` already submodules `claude-code-config`
  in practice; one submodule pointing at `shared-skills/` is the smallest possible
  topology. Option (a) adds a submodule edge to *both* consumers.
- **Versioned artifact for free.** Pinning a submodule to a release tag gives the
  consumer a stable, reproducible artifact without a separate packaging step.

## Consequences

### Positive

- Exactly one copy of each shared skill; drift is structurally impossible.
- Consumers update with one documented command (`git submodule update --remote`).
- `sdlc sync-check` gives a deterministic, hermetic parity verdict in CI or locally.
- Reuses the existing release workflow; each `vX.Y.Z` tag is a pinnable artifact.

### Negative / Trade-offs

- `claude-code-config` carries a coordination responsibility: a breaking change to
  a shared skill must consider the Codex consumer. Mitigated by the release tags
  (consumers pin and upgrade deliberately).
- The submodule pointer in `nix-install` must be bumped to adopt new skills; this
  is the explicit, auditable update step rather than silent propagation.

## Alternatives Considered

- **Option (a) — third `sdlc-shared-skills` repo**: cleanest separation of the
  shared set from either consumer, but it introduces a brand-new repo with its own
  CI, release workflow, and branch protections, plus a submodule edge on *both*
  consumers. Rejected on operational-overhead grounds; the shared skills already
  live here and this repo already releases.

## References

- `docs/stories/epic-07-external-controller.md` (Story 7.4-001)
- `docs/adr/001-controller-runtime.md`
- `shared-skills/README.md`
- `scripts/sync-shared-skills.sh`, `controller/src/sdlc/sync.py`
