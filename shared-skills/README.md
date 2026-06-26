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

## Harness-neutral sources (`neutral/`)

`neutral/<name>.skill.md` holds each shared skill in the **harness-neutral
definition format** (Epic-20, Story 20.4-001): YAML frontmatter with
harness-agnostic metadata plus a body that carries Claude-only constructs as
neutral placeholder tokens (`{{ARGUMENTS}}`, `{{SKILL_DIR}}`, `{{SHELL:cmd}}`)
and optional `<!-- harness:<name> --> … <!-- /harness -->` blocks. One neutral
source is the single thing authored; the Claude `SKILL.md` and the Codex
skill/manifest are generated from it.

The format, schema, and rationale are in
[`docs/adr/003-harness-neutral-skill-format.md`](../docs/adr/003-harness-neutral-skill-format.md).
The parser/validator/renderer is `controller/src/sdlc/skill_format.py`; the
frontmatter schema is
`controller/src/sdlc/schemas/neutral-skill.schema.json`. A controller test
renders every neutral source back to its live `*.md` body byte-for-byte, so the
two stay in lockstep.

### Authoring and regenerating skills (Story 20.4-002)

Author or edit a skill in exactly one place — `neutral/<name>.skill.md` — then
regenerate both harness files with the transpiler:

```bash
./scripts/generate-skills.sh generate
```

This drives `sdlc generate-skills`, which emits the Claude
`plugins/autonomous-sdlc/skills/<name>/SKILL.md` and the Codex mirror's
`plugins/autonomous-sdlc/skills/<name>/SKILL.md` from each neutral source. The
Claude body restores the `$ARGUMENTS` / `${CLAUDE_SKILL_DIR}` / `` !`…` ``
constructs; the Codex output carries the `.codex-plugin` manifest-schema
frontmatter (`metadata.short-description`) and the `Use <skill> …` invocation
forms. Pass explicit `CLAUDE_BASE` / `CODEX_BASE` arguments to target other
trees (defaults assume the `nix-install` submodule layout). The generator itself
is `controller/src/sdlc/skill_generator.py`.

The byte-parity `sdlc sync-check` below globs the top-level `*.md` skills only,
so the `neutral/` subdirectory does not affect it.

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
