# autonomous-sdlc (Claude Code plugin)

Claude-native mirror of the Codex `autonomous-sdlc` plugin. Packages the SDLC pipeline as a single namespaced bundle so skills are invoked as `/autonomous-sdlc:<name>`.

## Pipeline

```
project-init → brainstorm → generate-epics → create-epic → create-story → build-stories
                                                              ↑
                                                       (incremental story
                                                        additions to an
                                                        existing epic)
```

Plus `fix-issue` for triaging GitHub issues and `resume-build-agents` for resuming an interrupted multi-agent build.

## Skills

| Skill | Invocation | Purpose |
|---|---|---|
| `project-init` | `/autonomous-sdlc:project-init [name]` | Bootstrap a new repo: git init, GitHub remote, labels, lightweight `CLAUDE.md`, `PROJECT-SEED.md` for handoff to brainstorm. |
| `brainstorm` | `/autonomous-sdlc:brainstorm [idea]` | Senior-PM interview-driven requirements discovery, produces `REQUIREMENTS.md`. |
| `generate-epics` | `/autonomous-sdlc:generate-epics` | Bulk-generate epics + stories from an approved `REQUIREMENTS.md`. |
| `create-epic` | `/autonomous-sdlc:create-epic <NN> [topic]` | Interactively create a single new epic with stories. |
| `create-story` | `/autonomous-sdlc:create-story <NN> <description>` | Add one or more new stories to an existing epic. |
| `build-stories` | `/autonomous-sdlc:build-stories [story-id\|all]` | Multi-agent build pipeline that executes approved stories. |
| `fix-issue` | `/autonomous-sdlc:fix-issue [issue\|all\|next]` | Autonomous GitHub-issue triage and fix orchestration. |
| `resume-build-agents` | `/autonomous-sdlc:resume-build-agents` | Resume an interrupted parallel build run. |

## Codex symmetry

This plugin name and prefix match the Codex `autonomous-sdlc` plugin (`~/Documents/nix-install/plugins/autonomous-sdlc/`). The Claude side ships 8 of the 15 Codex skills — the user-facing SDLC chain. The remaining Codex skills (`check-releases`, `coverage`, `create-issue`, `create-project-summary-stats`, `plan-release-update`, `project-review`, `roast`) live as namespaced commands on the Claude side (`/devops:check-releases`, `/quality:coverage`, etc.) and are not duplicated here.

## Installation

The marketplace lives at the root of the parent repo (`~/dev/claude-code-config/.claude-plugin/marketplace.json`). After running `install.sh`, register and install with:

```
/plugin marketplace add fx-claude-config
/plugin install autonomous-sdlc@fx-claude-config
```

Verify with `jq '.plugins | keys' ~/.claude/plugins/installed_plugins.json` — `autonomous-sdlc@fx-claude-config` should appear in the list.

## Repository

Parent: <https://github.com/fxmartin/claude-code-config>
