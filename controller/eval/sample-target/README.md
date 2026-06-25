# sample-target

ABOUTME: Tiny throwaway repo the eval harness (Story 18.1-001) edits in isolation.
ABOUTME: Plain files (NOT a nested git repo) copied + `git init`-ed per eval run.

A minimal Python "string utilities" library used as the fixed target for the
agentic eval. The harness copies this directory into a throwaway workspace,
`git init`s it, then lets an agent work each ticket against the copy — the diff,
tokens, cost, wall-time, and `pytest` result are scored. The framework repo and
this template are never mutated.

Keep it small and stable: tickets in `../eval-config.yaml` reference the symbols
here, so a reproducible eval depends on this content staying versioned.
