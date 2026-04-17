# CLAUDE.md — A Deep Guide

**A worked explanation of Claude Code's most powerful file, dissected through the lens of `fxmartin/claude-code-config`.**

---

## Table of contents

1. [Why CLAUDE.md matters — the essay](#part-i--why-claudemd-matters-the-essay)
2. [How Claude Code discovers and loads it](#part-ii--how-claude-code-discovers-and-loads-it)
3. [Anatomy of this repo's CLAUDE.md — the worked example](#part-iii--anatomy-of-this-repos-claudemd-the-worked-example)
4. [Guardrails, defaults, focus — a taxonomy](#part-iv--guardrails-defaults-focus-a-taxonomy)
5. [Anti-patterns](#part-v--anti-patterns)
6. [Maintenance loop](#part-vi--maintenance-loop)
7. [Adapt for your project](#part-vii--adapt-for-your-project)
8. [Interaction with other Claude Code mechanisms](#part-viii--interaction-with-other-claude-code-mechanisms)

---

## Part I — Why CLAUDE.md matters (the essay)

### The problem

Every Claude Code session starts with no memory of how you work. Your naming conventions, your testing mandate, your tool preferences, your pet peeves about over-engineering — none of it exists in the model's context until something puts it there.

Without CLAUDE.md, you have exactly two places to put "how we work":

1. **The prompt**, each time. Every turn you pay the cost of re-explaining, you forget bits, and the signal dilutes. Worse, the rules drift session to session because you type them from memory.
2. **Implicit defaults in the model.** These are trained-in behaviors from Anthropic's training set — reasonable average engineer defaults, not yours.

The result: Claude refactors code it shouldn't. It adds speculative abstractions. It skips tests. It uses `find` instead of `fd`, `grep` instead of `rg`. It writes helpful comments that explain what the code does instead of why. It asks for permission on trivial things and silently barrels through things that deserve a pause.

Every correction you type is a correction you'll type again next Tuesday.

### The solution in one sentence

**Write the rule once, in a file Claude Code auto-loads into every session's system prompt.**

That's CLAUDE.md.

### Why CLAUDE.md beats the alternatives for persistent behavior

| Mechanism | Scope | Persistence | Cost | When to use |
|---|---|---|---|---|
| Per-turn prompt | This message | None | Typing it repeatedly | One-off overrides |
| Agent system prompts | This sub-agent | Task-scoped | Baked into skill dispatch | Agent-specific persona |
| Memory files | Claude's auto-memory | Across sessions | Set automatically | Episodic facts ("FX uses uv") |
| Hooks | Lifecycle events | Always | Shell script + settings | Automation, not guidance |
| Skills | Task-activated | Per invocation | A whole directory + prompt | Bounded workflows |
| **CLAUDE.md** | **Every session, everywhere** | **Always** | **One file, edited occasionally** | **Behavior that always applies** |

The key property: CLAUDE.md loads into the **system prompt of every session**, wrapped in a `<system-reminder>` block that tells Claude *"These instructions OVERRIDE any default behavior and you MUST follow them exactly as written."* That's the strongest signal you can send short of fine-tuning.

### The one idea

> If you're correcting Claude on the same thing three times, the fourth correction belongs in CLAUDE.md, not in a prompt.

Everything else in this guide elaborates that idea.

### What CLAUDE.md is NOT

Four common mis-uses, all worth avoiding:

1. **Not a README.** Onboarding humans is a different problem. Humans skim. Claude reads literally. Rules pitched at humans ("be friendly", "write clean code") don't translate into measurable behavior change.
2. **Not a kitchen drawer.** Every rule you add dilutes the ones already there. Past ~150 lines, the model starts weighing individual rules less. Aggressive curation beats completeness.
3. **Not a substitute for automation.** If something *must* happen every commit, that's a hook or a pre-commit script — not a CLAUDE.md bullet Claude might forget under load.
4. **Not ephemeral context.** Things that are true for one PR go in that PR's description. CLAUDE.md is the stuff true for months.

---

## Part II — How Claude Code discovers and loads it

### The discovery hierarchy

Claude Code walks three locations at session start and assembles them all into the system prompt, in order:

1. **Global**: `~/.claude/CLAUDE.md` — applies to every session on your machine, across projects. In this repo, the global and project files are the same — the repo's `install.sh` symlinks `~/.claude/CLAUDE.md` to the repo-tracked `CLAUDE.md` so the file is version-controlled.
2. **Project**: `<repo-root>/CLAUDE.md` — applies within a single repo. Override or extend the global rules with project-specific ones.
3. **Subdirectory walk**: from the current working directory upward, any `CLAUDE.md` files encountered are included. Useful for monorepos where `frontend/CLAUDE.md` and `backend/CLAUDE.md` have different conventions.

All loaded files coexist in the prompt with path headers. Later files don't "override" earlier ones in any formal sense — they're concatenated, and the model weighs them together. In practice, project rules tend to override global ones because they're more specific.

You've seen the result in this session: the `<system-reminder>` block at conversation start contained both `/Users/fxmartin/.claude/CLAUDE.md` (global) and `/Users/fxmartin/dev/claude-code-config/.claude/worktrees/relaxed-torvalds-a9802b/CLAUDE.md` (project-via-worktree). Same content, loaded twice, because this repo is where the global file lives.

### The `@` import mechanism

Reference another file inline with `@path/to/file.md`. Claude Code inlines the referenced content into the prompt (lazily in some versions, eagerly in others — check your CLI version).

Why this matters:

- **Modularity.** Your main CLAUDE.md stays under 100 lines. Deep references (Python patterns, database best-practices, testing guidelines) live in their own files.
- **Conditional depth.** A short CLAUDE.md sets the rules; the full Python style guide only matters when you're writing Python. `@docs/python-best-practices.md` pulls it in without bloating the base file.
- **Shareability.** If your `docs/testing-best-practices.md` is good, someone else can drop it into their setup without taking your whole CLAUDE.md.

This repo's CLAUDE.md uses `@` once at the bottom — `@~/.claude/reference-docs/source-control.md`. The rest are plain path references because they're loaded lazily by skills, not inlined at session start. Choose `@` when you want the content *in the system prompt of every session*; choose a plain path when you want a pointer the model or a sub-agent can follow on demand.

### The `<system-reminder>` wrapper

Claude Code wraps every loaded CLAUDE.md in markup that looks roughly like this:

```xml
<system-reminder># claudeMd
Codebase and user instructions are shown below. Be sure to adhere to
these instructions. IMPORTANT: These instructions OVERRIDE any default
behavior and you MUST follow them exactly as written.

Contents of /Users/fxmartin/.claude/CLAUDE.md (user's private global
instructions for all projects):

[your CLAUDE.md content here]
</system-reminder>
```

Three things to notice:

1. **"OVERRIDE any default behavior"** — explicitly empowers your rules over the model's trained defaults.
2. **"MUST follow them exactly"** — strong compliance signal. Use this authority carefully: every rule you write here competes for attention with the next one.
3. **Path headers are included** — when sub-agents see the prompt, they know which file each rule came from. If you want a rule to apply to sub-agents, put it in CLAUDE.md (not in a skill's per-invocation prompt).

### What loads, what doesn't

| Loads automatically into every session | Does NOT load automatically |
|---|---|
| Every `CLAUDE.md` in the discovery hierarchy | Skill files (activated per-invocation via `/name` or auto-match) |
| `@`-imported files referenced from CLAUDE.md | Hooks (fire on tool/session events, not read into context) |
| Auto-memory files (separate mechanism) | `settings.json` (affects harness behavior, not prompt) |
| | Agent definitions in `agents/*.md` (loaded per dispatch) |
| | Command files in `commands/*.md` (loaded on `/command` invocation) |

Rule of thumb: if the behavior must happen without Claude "deciding to", it's a hook or skill. If Claude should *know* about it and apply it contextually, it's CLAUDE.md.

---

## Part III — Anatomy of this repo's CLAUDE.md (the worked example)

Below, each section of [CLAUDE.md](../CLAUDE.md) in order, explaining what the rules are designed to prevent, what default they replace, and what a weak version of each would look like.

### Section 1: Core Principles (lines 3–10)

```markdown
## Core Principles
- Simple, clean, maintainable solutions over complex/clever implementations
- Surgical changes - smallest reasonable diff. Ask permission before
  reimplementing from scratch. Match existing style, even if you'd do
  it differently. If you notice unrelated dead code, mention it — don't
  delete it. Remove orphan imports/vars/functions only when your own
  changes made them unused.
- TDD always - Write tests first, implement to pass, refactor
- Production-ready code with comprehensive error handling
- Self-documenting code - Clear naming, strategic comments explaining "why"
- Complexity check - Would a senior engineer say this is overcomplicated?
  If yes, simplify.
- NEVER use --no-verify when committing
```

**What each buys:**

| Rule | Prevents | Replaces default |
|---|---|---|
| Simple/clean/maintainable | Clever-for-clever's-sake, pattern-matching to textbook designs | Claude's tendency to introduce design-patterns wholesale |
| Surgical changes | Scope creep, "while I'm here" refactors | Default LLM behavior to "improve" adjacent code it sees |
| TDD always | Tests-written-last ritual | Convenient default of writing code first, tests after |
| Production-ready | Toy examples in production code | Demo-grade outputs that skip error paths |
| Self-documenting | Comments that narrate the code | Habitual "// set x to 5" comments |
| Complexity check | Incremental over-engineering | Claude's uncalibrated complexity budget |
| NEVER `--no-verify` | Skipping pre-commit hooks under duress | Convenience shortcut when hooks fail |

**What a weak version would look like:**

```markdown
- Write good code
- Tests are important
- Keep it simple
```

These are true but untestable. Claude has no way to apply "good" as a rule. The strong version specifies behavior: *surgical diff*, *match existing style*, *senior-engineer check*, *never that specific flag*.

**Notice the hierarchy:** the Surgical changes bullet contains four sub-rules (match style, flag-don't-delete, clean your orphans, smallest diff). Compound rules are fine — a single bullet with sub-rules is read as one cohesive principle. Avoid splitting one idea into six separate bullets; it dilutes weight.

**Origin note:** the Surgical Changes sub-rules, Complexity check, and Verifiable Goals template (Section 4) were adapted from the Karpathy-guidelines repo. See the attribution footnote at the bottom of the file. Documenting provenance is a maintenance investment — six months from now you'll want to know why a rule is there.

### Section 2: Communication Style (lines 12–18)

```markdown
## Communication Style
- Address developer as **"FX"**
- Sharp, efficient, no-nonsense approach
- Business-minded with C-level context awareness
- Challenge when needed, push back on inefficiency
- Clear, structured responses with actionable insights
- ALWAYS ask for clarification rather than making assumptions.
- If you're having trouble with something, it's ok to stop and ask for help.
```

**What it shapes:** response tone and format. "Sharp, efficient, no-nonsense" produces terse replies with minimal preamble. "Challenge when needed" reduces sycophancy ("Great question!"). "ALWAYS ask for clarification rather than making assumptions" is the single biggest behavior-shaping rule most CLAUDE.md files miss — it flips Claude from "confidently guess" to "stop and check."

**Why name yourself:** Claude's default is to call you "the user" or nothing. Calling you "FX" personalizes the thread without theatrics. Useful when sub-agents produce reports — they'll address you by name, which reads less robotic.

**Pairing:** Communication Style is the *how*; Core Principles is the *what*. One shapes behavior in code, the other in prose. They're separate because the failure modes are different: verbose-but-correct-code and terse-but-sloppy-code are both real.

**Weak version:** "be concise" — untestable. Better: specify the anti-pattern ("no preamble, no cheerleading") or specify what you *do* want ("lead with the recommendation, then the rationale").

### Section 3: Code Quality Standards (lines 20–38)

Three subsections — **Python (uv + FastAPI)**, **TypeScript (Bun Runtime)**, **Testing (NO EXCEPTIONS)**.

**Why subdivide by language:** a rule like "comprehensive type hints" makes sense for Python but is meaningless for shell. Subdividing prevents the model from over-applying a rule out of context. When Claude writes Python, it pulls Python rules. When it writes TypeScript, it pulls TS rules.

**The Testing section** uses ALL CAPS ("NO EXCEPTIONS") and an explicit escape hatch:

```markdown
Authorization required: "I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME"
```

This is unusual and worth calling out. The pattern:

1. Make the rule absolute ("NO EXCEPTIONS")
2. Specify the exact phrase required to override

The result: Claude either writes tests or forces you to type a specific incantation that makes the override explicit and auditable. You can't accidentally skip tests. You can't vaguely wave them away. You have to *authorize* it, in exactly those words.

This pattern generalizes. Any rule you find yourself frustrated about exceptions to — bake in the escape hatch with an explicit phrase. It's the difference between "sometimes we skip tests" and "we skip tests only when FX types this sentence."

### Section 4: Workflow & Agents (lines 40–58)

Opens with the story-driven-development paragraph, then:

- `### Verifiable goals` subsection (Karpathy-inspired) — the numbered plan-with-verify template
- Inventory of available Agents (backend-typescript-architect, python-backend-engineer, etc.)
- Key skills list
- Integration patterns — condensed tags

**What it solves:** the orchestration layer. Core Principles tell Claude how to write code; Workflow tells Claude how to dispatch work. Two different problems.

**Verifiable Goals is doing heavy lifting here.** By telling Claude to "state a brief plan with explicit verification per step", you convert fuzzy tasks ("add validation") into loopable ones ("write tests for invalid inputs → verify they fail → make them pass → verify they pass"). Sub-agents given verifiable goals can run autonomously for much longer without needing clarification — because the stopping condition is mechanical.

**The Agents + Skills inventory** is worth calling out as an anti-pattern you avoided: you *could* duplicate each agent's full description here. You don't. You just name them. Sub-agents' full descriptions live in `agents/*.md` — CLAUDE.md just orients Claude to what's available. This is the right depth: enough that Claude knows to dispatch `ui-engineer` for frontend work, not so much that the list swallows the file.

### Section 5: cmux Observability (lines 60–62)

```markdown
## cmux Observability

Running on **cmux** — native macOS terminal for multi-agent AI development.
All workflow visibility is routed through `~/.claude/hooks/cmux-bridge.sh`
which provides graceful degradation (silent no-op if cmux unavailable).
See `docs/cmux-integration.md` for the full bridge API...
```

**What it solves:** environment-specific context. Claude needs to know: *cmux is the runtime, the bridge handles visibility, details are in the linked doc*. Three facts. No more.

**What it wisely omits:** the actual bridge API. Thirty-plus subcommands, flag details, hook semantics — all live in `docs/cmux-integration.md`. CLAUDE.md has a pointer, not the content.

**Pattern:** for platform/infra facts Claude needs at session start (so it doesn't suggest tools you don't use), inline 2–3 sentences. For the full reference, link out.

**Weak version:** "we use cmux for observability." Doesn't tell Claude where to look when it needs to emit a status update. The strong version names the specific bridge script path.

### Section 6: CLI Tools (lines 64–83)

A table mapping legacy tools (`find`, `grep`, `cat`, `cd`) to modern replacements (`fd`, `rg`, `bat`, `z`). Followed by three rules:

```markdown
- Prefer Claude Code's built-in tools (Read, Grep, Glob, Edit) for direct
  file operations
- Use these CLI tools via Bash when you need shell pipelines...
- In shell scripts and automation, always use `fd`/`rg`/`bat`/`jq`...
```

**Why a table:** the substitutions are paired. Rules in the form "instead of X, use Y" are easier to read as a table than as bullets. Tables also let the Why column carry the rationale without breaking flow.

**Why the three rules below the table:** the table says *what*, the rules say *when*. `fd` is better than `find` — but not better than the built-in Grep tool for simple searches. Without the priority rules, Claude would shell out to `fd` even when `Grep` would be faster and give you better visibility.

**Hidden guardrail:** `**Always use `scc` for any LOC counting task.**` and `**Always use `typst` for PDF generation.**` The **bold** emphasis acts as a mini-OVERRIDE — these are the ones FX has been burned on most often (Claude defaults to `wc -l` or `reportlab`, both wrong for this stack).

### Section 7: GitHub Operations (lines 85–92)

```markdown
- Always use `gh` CLI for all GitHub operations
- Do NOT rely on a GitHub MCP server — it has been removed...
```

**What it prevents:** Claude searching for a `github` MCP tool that used to exist, hitting 404s, falling back to unclear error messages. Explicit negative instruction (*"Do NOT rely on..."*) is sometimes the only way to disable a default behavior.

**Pattern:** when you remove a dependency (MCP server, SDK, tool), add a negative-instruction sentence to CLAUDE.md. Claude's training set still remembers the old tool; without the explicit prohibition it'll keep reaching for it. Over time this section accumulates (and you prune) as your stack evolves.

### Section 8: Reference Materials (lines 94–100)

```markdown
- **Python**: `docs/python-best-practices.md`
- **Database**: `docs/database-best-practices.md`
- **Containers**: `docs/container-best-practices.md`
- **Testing & TDD**: `docs/testing-best-practices.md`
- **Source Control**: `@~/.claude/reference-docs/source-control.md`
- **Full Workflow**: `WORKFLOW.md` and `WORKFLOW-v2.md`
```

Five pointers, one `@` import. The `@` imports Source Control eagerly into the prompt — because commit/branch/PR conventions apply in every session. The other four are lazy pointers — Claude knows they exist and reads them only when relevant (writing Python code, designing a DB schema, etc.).

**Why not `@` everything:** context budget. Eagerly inlining five reference docs at session start would burn tokens on rules that don't apply 80% of the time. Lazy pointers cost almost nothing until invoked.

### Section 9: Attribution footnote (line 103)

```markdown
---
*Surgical Changes rules, Complexity check, and Verifiable Goals template
adapted from [forrestchang/andrej-karpathy-skills](https://github.com/...)
(MIT), itself derived from Andrej Karpathy's observations on LLM coding
pitfalls.*
```

**Why include provenance:** six months from now, when you're deciding whether to keep or cut the "Complexity check" bullet, the footnote tells you where it came from. Upstream may have evolved. You may want to re-sync.

**Licensing:** adopted content was MIT-licensed, which permits this use. If you absorb rules from a differently-licensed source, check compatibility.

---

## Part IV — Guardrails, defaults, focus — a taxonomy

A well-tuned CLAUDE.md does four jobs. Most rules do exactly one. Worth knowing which.

### 1. Guardrails (things it PREVENTS)

| Rule in this file | Prevents |
|---|---|
| `NEVER use --no-verify` | Skipping pre-commit hooks |
| `Do NOT rely on a GitHub MCP server` | Using removed tooling |
| `No abstractions for single-use code` (implicit in Surgical Changes) | Premature generalization |
| `Don't refactor things that aren't broken` | Scope creep |
| `No features beyond what was asked` | Speculative work |

Guardrails are the highest-ROI rules. They convert "Claude did a bad thing" → "Claude doesn't do the bad thing." Every correction you've typed twice becomes a candidate guardrail.

### 2. Defaults (things it MAKES the default)

| Rule | New default |
|---|---|
| TDD always | Tests before implementation |
| Use `uv` for Python | Not `pip`/`poetry`/`pipenv` |
| Use `bun` for TS runtime | Not `node`/`npm` |
| Use `gh` CLI for GitHub | Not API-via-curl, not MCP |
| Use `fd`/`rg`/`bat` | Not `find`/`grep`/`cat` |

Defaults save keystrokes. Without them, every session Claude asks "npm or yarn?" or defaults to the wrong one. With them, Claude picks your tool silently.

### 3. Focus (things it PRIORITIZES)

| Rule | Priority shift |
|---|---|
| Smallest reasonable changes | Size of diff > cleverness of refactor |
| Self-documenting code | Naming > comments |
| Production-ready code | Error handling > happy path |
| Complexity check | "Would senior engineer say overcomplicated?" > "Is it technically complete?" |

Focus rules tilt tradeoffs. They don't prevent or mandate — they tell Claude which axis to optimize when options exist.

### 4. Communication (how Claude addresses you)

| Rule | Shape |
|---|---|
| Address developer as FX | Personal, not generic |
| Sharp, efficient, no-nonsense | Terse, front-loaded |
| Challenge when needed | Less sycophantic |
| ALWAYS ask for clarification | Fewer guessed assumptions |

Communication rules are easy to under-invest in because they feel superficial. They're not — they change your hourly experience more than any code rule. If Claude's default tone annoys you, fix it here.

### The fifth implicit category: Knowledge

CLAUDE.md also tells Claude what's in your stack — cmux, uv, bun, Podman, specific agents, specific skills. This is knowledge injection, not a rule. It shapes which defaults Claude reaches for even when no rule applies. You write it in the form of a list ("Agents: backend-typescript-architect, python-backend-engineer, ...") and the model treats it as context.

---

## Part V — Anti-patterns

### Anti-pattern 1: Kitchen-sink CLAUDE.md

Past ~150 lines, rule attention dilutes. Every rule you add weakens the others. The counter-intuitive implication: adding a rule can cause the model to follow existing rules less well.

**Symptom:** you added rule X. Now Claude starts violating rule Y, which it used to follow.

**Fix:** audit quarterly. Remove rules that haven't triggered in 3 months. Combine related rules into one compound bullet (like this file's Surgical Changes).

### Anti-pattern 2: Cargo-culting from other repos

Someone else's CLAUDE.md reflects *their* pain points. Copy-pasting their rules transfers the prose without transferring the experience that justified each rule. You end up with rules you don't remember why you added.

**Symptom:** you can't explain why a specific bullet is there.

**Fix:** when you see a great rule in someone else's CLAUDE.md, don't copy it. Instead, note the *problem* it solves. If you have that problem, craft your own rule for it. If you don't, skip.

### Anti-pattern 3: Drift between CLAUDE.md and reality

Your CLAUDE.md says "TDD always." Your actual workflow has 17 places where you `/fix-issue --skip-coverage`. The rule has decayed into a lie.

**Symptom:** reviewing a PR, you find yourself thinking "the tests aren't really testing anything" but CLAUDE.md still mandates them.

**Fix:** either strengthen enforcement (remove the skip flag from common skills) or weaken the rule ("TDD for new features; bug-fix tests optional for CSS-only PRs"). Don't leave the rule as a pious fiction.

### Anti-pattern 4: Using CLAUDE.md for things hooks should do

CLAUDE.md can't force; it suggests strongly. If something *must* happen every commit, don't write "Claude should always run tests before committing" — write a pre-commit hook.

**Heuristic:**

- If Claude can forget it → OK for CLAUDE.md
- If it must always happen even when Claude isn't involved → hook or Git action, not CLAUDE.md

### Anti-pattern 5: Writing for human maintainers vs. writing for Claude

Humans skim. Claude reads literally but weighs tone against instruction density. Rules that work for a human README (`## Getting Started\n1. Clone the repo\n2. Install deps...`) are wasted on Claude — it doesn't "onboard", it just applies rules per session.

**Fix:** separate files. `README.md` for humans, `CLAUDE.md` for Claude. Both can exist. They serve different audiences.

### Anti-pattern 6: Rules Claude literally can't follow

`"Be creative"`, `"think outside the box"`, `"use good judgment"`. These aren't rules — they're wishes. Claude has no referent for "good judgment"; without a testable criterion, the phrase just takes up space.

**Fix:** if you catch yourself writing a feel-good rule, ask *"how would I test whether Claude followed this?"* If you can't articulate the test, the rule doesn't belong.

### Anti-pattern 7: Unlimited escape hatches

Every escape hatch you add ("unless the task is complex", "except for prototypes", "unless you're sure") is a license to skip the rule. If you want exceptions, make them explicit and rare — like this repo's Testing section with its single, specific override phrase.

---

## Part VI — Maintenance loop

### When to add a rule

The heuristic in one line: **three corrections of the same mistake = a rule.**

Variations:

- You've typed "use fd, not find" in three different sessions → add it to CLI Tools.
- You've said "don't refactor that" on three PRs → add it to Surgical Changes.
- A sub-agent returned results in a format you had to re-parse three times → add output-contract rules to that agent's prompt, or generalize into CLAUDE.md.

Before adding: check whether an existing rule covers it. Adding the same rule twice (in different words) is worse than adding it once in the right place.

### When to remove a rule

- **Never-trigger**: quarterly, check which rules would change Claude's behavior if deleted. If a rule is trivially followed by default, remove it.
- **Superseded**: you added a stronger rule that covers the weaker one.
- **Dead tool**: the thing the rule targeted is no longer in your stack. (Your removed-MCP-server GitHub rule is the inverse: the tool is gone, the rule stays as a negative instruction. That's fine when the model might still try to reach for the old thing.)
- **Drifted**: rule and reality diverge, and you've decided reality wins.

### Detecting drift

Two practical checks:

1. **Grep your skills for bypasses:** `rg '\-\-skip' skills/` — find flags that explicitly bypass CLAUDE.md rules. Count them. If skip-this-rule is more common than follow-this-rule, the rule is dead.
2. **Review one week of sessions:** re-read a handful of recent conversations. Find three places where Claude did something you then corrected. Are those corrections captured in CLAUDE.md?

### Version control

Always check in CLAUDE.md. Always commit changes with a message explaining *why* the rule was added:

```
feat: add Complexity check heuristic

Added after reviewing PR #234 where Claude proposed a factory for
a single call site. The "would a senior engineer say overcomplicated"
question would have caught it earlier.
```

Future-you needs the *why*. The rule itself is searchable; the rationale isn't.

### Install pattern

This repo's `install.sh` symlinks `~/.claude/CLAUDE.md` → the repo-tracked `CLAUDE.md`. That means every edit is version-controlled the instant you save, and the global-scope rules live in git. If you instead copy the file (no symlink), drift becomes inevitable.

Symlinks > copies for any file that should stay canonical.

---

## Part VII — Adapt for your project

Starting from scratch? Here's the minimum viable CLAUDE.md and the questions that generate each section.

### Template

```markdown
# <Project> — Claude Instructions

## Core Principles
<3–7 bullets describing how code gets written in this project>

## Communication Style
<3–5 bullets describing how you want Claude to respond>

## Code Quality Standards
### <Primary language>
<tooling choices, conventions>
### Testing
<what level of tests, when, with what framework>

## Workflow
<how work gets dispatched — skills, agents, sub-tasks>

## Tooling
<preferred CLIs, forbidden tools, environment notes>

## References
<pointers to deeper docs, @-imports for always-loaded guides>
```

### Questions that generate each section

Ask yourself, concretely:

**For Core Principles:** *What are the three corrections I'd most hate to type again?*
Every answer becomes a bullet.

**For Communication Style:** *When Claude's responses annoy me, what's the pattern?*
Too verbose? Too much preamble? Too hedgy? Turn the annoyance into its opposite, as a rule.

**For Code Quality:** *What's my stack? What conventions are implicit?*
Write them down even if they feel obvious. "We use TypeScript" seems too basic to include, until Claude suggests a JavaScript idiom.

**For Testing:** *What's the minimum test coverage I'd stop a PR for?*
That's the rule. Not "tests are important" — the specific threshold.

**For Workflow:** *How do I dispatch complex work today? Agents? Sub-tasks? Just long prompts?*
Document the actual pattern, not the aspirational one.

**For Tooling:** *What tools have I installed but Claude keeps ignoring?*
`fd` instead of `find`, `rg` instead of `grep`, specific formatters. Name them.

**For References:** *What docs do I wish Claude re-read more often?*
Link them. `@`-import the ones that apply universally; leave the rest as plain paths.

### Test your CLAUDE.md

Write a prompt that should succeed by following your rules, and one that should be rejected or flagged. Run both. If Claude handles them as expected, the rules are working.

Example tests for this repo:

- **Should succeed:** "Add a FastAPI endpoint for user login." → Claude uses `uv`, writes tests first, adds type hints, produces production-ready error handling.
- **Should flag:** "Quick PoC, skip the tests." → Claude asks for the exact override phrase ("I AUTHORIZE YOU TO SKIP WRITING TESTS THIS TIME"), doesn't quietly comply.
- **Should reject:** "Use the GitHub MCP tool to create an issue." → Claude falls back to `gh issue create`, per the negative instruction.

A CLAUDE.md that passes these tests is earning its keep.

---

## Part VIII — Interaction with other Claude Code mechanisms

### CLAUDE.md vs. Skills

| Use CLAUDE.md when... | Use a skill when... |
|---|---|
| Behavior applies across all tasks | Behavior is task-scoped |
| Rule is a principle or preference | Rule is a workflow (multiple steps) |
| Content is static | Content needs parameterization (`{{ARGUMENTS}}`) |
| Scope = everything | Scope = when user types `/skill-name` or matches auto-trigger |

Example: *"Always use uv for Python"* → CLAUDE.md. *"Fix a GitHub issue in N phases"* → skill.

### CLAUDE.md vs. Hooks

| Use CLAUDE.md when... | Use a hook when... |
|---|---|
| Claude should know about it | It must fire without Claude's involvement |
| Soft enforcement via prompt is enough | Hard enforcement is required |
| Graceful degradation is acceptable | Failure must block |

Example: *"Commit messages should include Fixes #N"* → CLAUDE.md rule. *"Run tests before every commit"* → pre-commit hook.

### CLAUDE.md vs. Memory (auto-memory)

| Use CLAUDE.md when... | Use memory when... |
|---|---|
| Rule is deliberate | Fact is episodic |
| Rule should be version-controlled | Fact is incidental |
| You want it to apply to every session | You want Claude to learn it over time |

Example: *"Run tests before committing"* → CLAUDE.md. *"FX prefers dark mode"* → memory.

Memory is for things Claude should notice and remember; CLAUDE.md is for things you've decided and encoded.

### CLAUDE.md vs. Agent definitions

Sub-agents defined in `agents/*.md` have their own prompts. They also see CLAUDE.md (it's in the system prompt of every session, including sub-agent sessions). Rules in CLAUDE.md apply to sub-agents for free.

**Implication:** don't duplicate CLAUDE.md rules into agent prompts. If "TDD always" is in CLAUDE.md, every agent inherits it. Agent prompts should cover *task-specific* guidance (what this agent's job is, what its output contract is, what tools it should prefer), not re-state the global rules.

### Settings.json

`settings.json` affects the **harness**: permissions, allowed tools, hooks, model selection, env vars. It does not affect Claude's prompt. The two files complement each other — CLAUDE.md says what Claude should *do*; settings.json says what the runtime *lets it* do.

Example: CLAUDE.md says "use `gh` CLI". settings.json may need `"Bash(gh *)"` in allowed-tools so Claude can actually run it without a permission prompt. Neither file does the other's job.

### Precedence (informal)

When conflicts arise — rare, but happen — the rough order Claude weights by:

1. **Per-turn user prompt** (highest — you're telling Claude *now*)
2. **Skill instructions** (when a skill is active)
3. **CLAUDE.md** (the OVERRIDE-default layer)
4. **Agent system prompts** (when dispatched)
5. **Harness defaults** (lowest — Claude's trained-in behavior)

Your per-turn prompt can override CLAUDE.md for this turn. CLAUDE.md overrides the trained-in defaults for every turn. Understanding the stack helps you place rules at the right level.

---

## Closing

CLAUDE.md is the file where deliberate design outranks accidental habit. Written carefully, it turns every session into one where Claude starts already aligned to your standards. Written lazily, it's a neglected README that Claude technically reads but doesn't learn anything useful from.

The work is the curation: a short file of sharp rules, audited quarterly, owned in version control, grounded in the corrections you got tired of typing.

Six months from now, when a new teammate joins or you spin up a new project, this file is what you'd pass to them. That's the test. If it wouldn't help a senior engineer calibrate to your standards, it won't help Claude either.

---

## Further reading

- This repo's [`CLAUDE.md`](../CLAUDE.md) — the file this guide dissects
- [`docs/cmux-integration.md`](cmux-integration.md) — example of a deep-dive that CLAUDE.md links to rather than inlines
- [`WORKFLOW.md`](../WORKFLOW.md), [`WORKFLOW-v2.md`](../WORKFLOW-v2.md) — the multi-agent workflow CLAUDE.md orients Claude toward
- [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) — upstream for three of this file's rules
- Anthropic's [Claude Code documentation](https://docs.claude.com/en/docs/claude-code) — authoritative reference for CLAUDE.md loading behavior and harness mechanics

---

*This guide is intentionally opinionated. It reflects one person's hard-won rules for one workflow. Your mileage will vary; the framework shouldn't.*
