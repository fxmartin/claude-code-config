# Over-Engineering Review Lens

The over-engineering lens operationalizes the `CLAUDE.md` complexity-check
("smallest reasonable diff / would a senior engineer say this is
overcomplicated?") *inside* the autonomous pipeline, where no human applies it.
On each story's diff it asks an LLM for a structured **delete-list** of
over-built code and then routes that list per a configurable policy.

It is a review **dimension**, not a new stage — it composes with the existing
review stage / Epic-08 adversarial reviewer slot rather than adding a fifth
stage to `build → coverage → review → merge`.

Introduced in Story 18.2-001 (Epic-18). Disabled by default.

## What it flags

The lens returns a delete-list of genuinely over-built code, each with a
file, line, category, and a one-line "why":

- `speculative_abstraction` — a factory/hook/interface wrapping a single call site
- `unused_code` — params, branches, or helpers nothing reaches
- `reinvented_wheel` — hand-rolled code a stdlib / existing dep / one-liner covers
- `premature_generality` — config/extensibility no caller needs yet
- `other` — anything else over-built

On an already-minimal diff it returns an **empty** findings list and says so —
it does not nitpick code that is already lean (low false-positive rate).

## Output contract

The lens emits this shape (JSON-schema draft 2020-12, published at
`controller/src/sdlc/schemas/overengineering-lens-response.schema.json`):

```json
{
  "summary": "Two speculative abstractions; rest of the diff is lean.",
  "findings": [
    {
      "category": "speculative_abstraction",
      "file": "src/cache.py",
      "line": 42,
      "reason": "CacheFactory wraps one constructor; inline it"
    },
    {
      "category": "unused_code",
      "file": "src/api.py",
      "line": null,
      "reason": "`legacy` flag is never read"
    }
  ]
}
```

`line` is `null` for file-level findings. An empty `findings` array means the
diff is already minimal.

In Python the validated list is lifted into frozen `Finding` dataclasses by
`extract_findings`, in `controller/src/sdlc/overengineering.py`.

## Policy

Configured in `controller/config/overengineering-lens.yaml`:

```yaml
enabled: false          # master switch — off = behaviour unchanged from before
policy: advisory        # advisory (default) | route_to_simplify
command: "overengineering-lens.sh --pr-number {pr_number}"
timeout_sec: 300
```

`route_findings` maps findings + config to one of four outcomes:

| Condition                          | Action               | Effect |
|------------------------------------|----------------------|--------|
| `enabled: false`                   | `disabled`           | Lens never runs; findings dropped; behaviour unchanged. |
| Enabled, no findings               | `clean`              | Stay quiet — no PR comment on an already-lean diff. |
| Enabled, findings, `advisory`      | `advisory`           | Record the delete-list as an advisory PR comment; **never blocks shipping**. |
| Enabled, findings, `route_to_simplify` | `route_to_simplify` | Hand the cuts to the bounded bugfix loop; the agent applies them and the gates re-run. |

`advisory` is the default so the lens never gates shipping on style. The
`route_to_simplify` path reuses the bounded bugfix loop — `LensOutcome`'s
`simplify_directive()` renders the cuts as a failure-style directive the loop
acts on, exactly as a failed gate would.

The command template accepts `{pr_number}`, `{pr_url}`, and `{story_id}`
placeholders; the wrapper fetches the PR diff, runs a `simplify`/`roast`-style
delete-list pass, and emits the schema JSON above. Swapping the lens runtime is
a config change here, not orchestrator code.

## Disable switch

`enabled: false` (the default) short-circuits `dispatch_overengineering_lens`
before any command runs — no quota is spent and the pipeline behaves exactly as
it did before this story.

## How it's verified

The eval harness (Story 18.1) is how we confirm the lens actually reduces net
LOC on over-build-prone stories without lowering gate pass rates. The lens
itself is unit-tested for finding extraction and policy routing in
`controller/tests/test_overengineering.py`, including lean-diff (stays quiet)
and over-built-diff fixtures.

## Relationship to other tooling

- Mirrors ponytail's `/ponytail-review` "delete-list" output and the built-in
  `simplify` / `roast` skills — but runs *in* the autonomous pipeline.
- Complements the Epic-08 [adversarial review slot](adversarial-review.md): the
  adversarial slot gates on correctness/security; this lens advises on
  simplicity and never blocks by default.
