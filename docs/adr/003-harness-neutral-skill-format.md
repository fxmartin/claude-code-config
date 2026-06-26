<!-- ABOUTME: ADR-003 recording the harness-neutral skill definition format that extends ADR-002. -->
<!-- ABOUTME: Decision: one neutral source (frontmatter + body with placeholders) generates Claude and Codex skills. -->

# ADR-003: Harness-Neutral Skill Definition Format

- **Status**: Accepted
- **Date**: 2026-06-26
- **Epic / Story**: Epic-20 / Story 20.4-001
- **Deciders**: FX
- **Extends**: [ADR-002](002-codex-mirror-sync.md)

## Context

ADR-002 made `claude-code-config` the single source of truth for the **seven
shared skills** and removed duplication by having the `nix-install` Codex mirror
consume `shared-skills/` as a git submodule. That solved drift for skills whose
*body is byte-identical* across runtimes.

Epic-20 widens the goal: run the whole SDLC pipeline on any harness and author
each pipeline skill **once**. The two targets do not share a file shape:

- **Claude** wants a `SKILL.md` with YAML frontmatter — `name`, `description`,
  `allowed-tools`, `argument-hint`, `disable-model-invocation`, `user-invocable`
  — and a body that may use the Claude-only constructs `$ARGUMENTS`,
  `${CLAUDE_SKILL_DIR}`, and the `` !`…` `` shell preprocessor.
- **Codex** wants a `.codex-plugin` manifest entry plus a `Use <skill>` body
  whose argument convention and lack of a skill-dir / shell preprocessor differ.

Hand-porting one to the other is exactly the drift ADR-002 set out to kill, now
re-appearing one layer up. Before a generator (Story 20.4-002) or a parity gate
(Story 20.4-003) can exist, the **neutral source format** they operate on has to
be defined and proven able to express the existing shared skills without loss.

## Decision

**A skill is authored once as a neutral source: harness-agnostic YAML
frontmatter plus a body that carries Claude-only constructs as neutral
placeholder tokens (and, where needed, harness-tagged blocks). One source
generates both the Claude `SKILL.md` and the Codex skill/manifest.**

The format is:

- **Frontmatter** — validated against
  `controller/src/sdlc/schemas/neutral-skill.schema.json` (draft 2020-12).
  Harness-neutral keys (`snake_case`, not Claude's `kebab-case`) capture the
  *union* of what each target needs: `name`, `description`, `short_description`,
  `argument_hint`, `allowed_tools`, `model_invocation` (`auto` | `disabled`, the
  neutral form of Claude's `disable-model-invocation`), `user_invocable`,
  `invocation_examples` (the Codex `Use <skill> …` forms), and `harnesses` (the
  targets to generate for; Claude-only skills list just `["claude"]`).
- **Body** — markdown with two neutral mechanisms the generator translates or
  omits per target:
  - **Placeholder tokens** for the three named Claude-only constructs:
    `{{ARGUMENTS}}` → `$ARGUMENTS`, `{{SKILL_DIR}}` → `${CLAUDE_SKILL_DIR}`,
    `{{SHELL:cmd}}` → `` !`cmd` ``. On a harness without these, they are dropped
    (Codex receives arguments via its `Use <skill> …` invocation).
  - **Harness-tagged blocks** — `<!-- harness:claude --> … <!-- /harness -->` —
    for whole sections that should appear for one harness only.

The parser, validator, serializer, and per-harness body renderer live in
`controller/src/sdlc/skill_format.py`. The seven shared skills are committed as
neutral sources under `shared-skills/neutral/<name>.skill.md`; a test renders
each back to its live `shared-skills/<name>.md` body byte-for-byte, proving the
format is lossless and acting as a parity guard until the generator and CI gate
land.

This story defines and proves the **format** only. Emitting whole skill *files*
(the Claude `SKILL.md` and the Codex manifest) is the generator's job (Story
20.4-002); failing CI on drift is the parity gate's job (Story 20.4-003).

## Rationale

- **Extends, not replaces, ADR-002.** `claude-code-config` stays the source of
  truth and the Codex mirror still consumes `shared-skills/` as a submodule. The
  neutral sources are additive (`shared-skills/neutral/`), and the byte-parity
  `sdlc sync-check` — which globs the top-level `*.md` only — is unaffected.
- **One format, both targets.** Snake-case neutral keys avoid privileging
  Claude's spelling; the generator maps them to each harness's idiom.
- **Lossless by construction.** The only Claude-only construct the seven shared
  skills use today is a trailing `$ARGUMENTS`; representing it as `{{ARGUMENTS}}`
  lets `render_body(skill, "claude")` reproduce the original body exactly.
- **Forward-compatible.** Adding a harness means extending `KNOWN_HARNESSES`
  and the placeholder map — no change to the format or the sources.

## Consequences

### Positive

- A skill is authored once; Claude and Codex outputs derive from it (20.4-002).
- The render-back test is a built-in parity guard: editing a shared skill body
  without regenerating its neutral source fails the controller test suite.
- The schema rejects authoring mistakes (unknown keys, non-kebab names, bad
  enums) at parse time with actionable, field-named errors.

### Negative / Trade-offs

- Until Story 20.4-002 lands, the live `shared-skills/<name>.md` bodies and their
  neutral sources are maintained in tandem; the render-back test keeps them
  honest but the generator is what removes the duplication for good.
- The neutral source duplicates each body once more in the tree. Accepted as the
  bridge to the generator; 20.4-002 makes the neutral source the sole authored
  artifact.

## Alternatives Considered

- **Keep two hand-written copies (status quo before Epic-20).** Rejected: it is
  precisely the drift ADR-002 set out to eliminate, re-emerging at the
  per-harness file level.
- **Make the Claude `SKILL.md` itself the neutral source.** Rejected: its
  frontmatter spelling and `$ARGUMENTS` / `${CLAUDE_SKILL_DIR}` / `` !`…` ``
  constructs are Claude-specific, so it cannot losslessly describe a Codex skill
  without a translation layer — which is exactly the neutral format.
- **A bespoke binary/JSON skill object instead of frontmatter+markdown.**
  Rejected: markdown-with-frontmatter is already the idiom both harnesses use and
  keeps sources human-authorable and diff-friendly.

## References

- `docs/adr/002-codex-mirror-sync.md`
- `docs/stories/epic-20-cross-harness-portability.md` (Story 20.4-001)
- `controller/src/sdlc/skill_format.py`
- `controller/src/sdlc/schemas/neutral-skill.schema.json`
- `shared-skills/neutral/`, `shared-skills/README.md`
