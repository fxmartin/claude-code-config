<!-- ABOUTME: Story 34.4-002 prompt audit of stage prompts and plugin skills for previous-generation steering -->

# Prompt Audit — current-generation models (2026-09)

Story 34.4-002 (Epic-34). Reading exercise over the prompts the controller
dispatches and the plugin skills, looking for instructions written for the
previous model generation that now reduce output quality. Scope:

- `render_*_prompt` in `controller/src/sdlc/build.py` and `fix_issue.py`
- `contracts._result_wrapper` (`controller/src/sdlc/contracts.py`)
- `plugins/autonomous-sdlc/skills/*/SKILL.md`

Patterns searched: "think step by step"/scratchpad/thinking-tag rules, "do not
reason" clauses, forced `tool_choice` assumptions, over-prescriptive step lists,
mandatory progress-narration between tool calls, prefilled-response tricks.

## Verdict

**No rule is removed.** The prompts contain none of the hard obsolete patterns,
and every process rule that looks "dated" traces to a recorded incident or an
Epic-26 pressure test. The diff is the report plus
`controller/tests/test_prompt_audit_34_4_002.py`, which pins the keep-list and
guards against the obsolete patterns being introduced.

## Clean checks (no finding)

| Pattern | Result |
|---|---|
| "think step by step" / `<thinking>` / `<scratchpad>` rules | Absent from every audited file. |
| "do not reason" / "answer directly" clauses | Absent. |
| Forced `tool_choice` | Absent. Result extraction is text-based: `contracts.parse_and_validate` scans the reply for the `<<<RESULT_JSON>>>` sentinel block (with fenced/bare-object fallbacks). It never reads a tool-call argument, so the removal of forced tool choice does not affect it. |
| Prefill / "begin your reply with" | Absent. `_result_wrapper` asks the agent to *end* with the block, which does not need prefill. |
| Progress narration between tool calls | No prompt requests it. See F-1 for the one watch item. |

## Findings

| ID | Location | Pattern | Why possibly obsolete | Confidence | Disposition |
|---|---|---|---|---|---|
| F-1 | `contracts.py:390-395` (`_result_wrapper` "End your reply with EXACTLY…, nothing after the closing marker") | Output-format pinning | The one rule that interacts with Opus 5.5's between-tool-call progress updates: updates mid-session are fine, only the *final* reply must end on the block, and the sentinel parser takes the last well-formed block. | High that it is still needed | **Keep.** Reworded nothing; covered by `test_result_wrapper_*` in `test_build.py` and the new keep-list test. Re-measure final-reply format compliance in 34.4 eval runs before any change. |
| F-2 | `contracts.py:396-397` ("Use these exact keys. Enum fields must be one of the literals shown.") | Key/enum drift guard | Newer models paraphrase keys less, but the docstring records the `branch` vs `branch_name` / `PASSED` vs `SUCCESS` failure; stage success depends on it. | High (keep) | **Keep.** |
| F-3 | `contracts.py:406-411` ("one-shot headless session…") | Session-model rule | Incident run b8fdbc71 (story 27.1-003 merge attempt 3): background wait + scheduled wakeup ended the turn with no result block. | High (keep) | **Keep, cited** (AC3). Test: `test_result_wrapper_forbids_background_handoff`. |
| F-4 | `build.py:5117-5120` (`render_merge_drift_block`, `CR_NONE` branch: "Do not poll, sleep, or wait") | No-pipeline wait | Issue #740: agent invented a pipeline wait and reported a false FAILED that blocked 13 dependents. | High (keep) | **Keep, cited.** Tests: `test_merge_prompt_no_ci_status_forbids_pipeline_wait`, `test_render_merge_prompt_no_ci_status_forbids_pipeline_wait`. |
| F-5 | `build.py:5134-5137` (blocking foreground watch) | Synchronous wait | Same incident as F-3 on the merge prompt. | High (keep) | **Keep, cited.** Test: `test_merge_prompt_requires_synchronous_check_wait`. |
| F-6 | `build.py:5102-5110`, `fix_issue.py` merge prompt (do not rebase; `git merge origin/main`) | Prescriptive git procedure | Issue #738 replay-conflict failure. | High (keep) | **Keep, cited.** Test: `test_merge_prompt_merges_as_is_and_never_rebases`. |
| F-7 | `build.py:4928-4931`, `fix_issue.py:722-725` ("Never run `git stash`") | Forbidden-action rule | Issue #590 stranded stash. | High (keep) | **Keep, cited.** Tests in `test_dirty_tree_guard.py`. |
| F-8 | `build.py` merge prompt (`merged_at`/`merge_sha` "never null", `build.py:5194`), `fix_issue.py:860` | Schema-gap note | Story 29.1-001, run 8e16140c. | High (keep) | **Keep, cited.** |
| F-9 | `build.py:5739-5754` (`render_bugfix_prompt`: root-cause-first, "Refuse the shortcuts", six-step reception sequence) | Over-prescriptive step list | Reads as a step list a newer model might follow unprompted. But it is the Epic-26 (26.1-001, 26.2-001) RED/GREEN pressure-test output; the shortcuts quoted are the rationalisations observed under pressure, which stronger models still exhibit. | Medium that it is over-specified; high that it must stay until re-pressure-tested | **Keep.** Candidate for a trial simplification only after re-running the Epic-26 pressure scenarios on Sonnet 5.5 / Opus 5.5. |
| F-10 | `build.py:5076-5079`, `fix_issue.py:790-793` ("Do not trust the implementer's report") | Verification rule | Story 26.2-002 pressure-tested rule. | High (keep) | **Keep.** |
| F-11 | `build.py:4915-4940` (`render_build_prompt` numbered steps 1-6), `fix_issue.py:713-738` | Numbered step list | Steps are the controller's contract (branch name, commit header, push ownership), not reasoning guidance. "Follow TDD" is one line, not a procedure. | High (keep) | **Keep.** |
| F-12 | `fix_issue.py:662-668` (`render_investigation_prompt`: steps 1-4 + complexity rubric) | Over-prescriptive step list | Steps 1-4 (extract, search, determine root cause, assess risk) are what a current model does by default. The LOW/MEDIUM/HIGH rubric is load-bearing (HIGH escalates the model). | Medium | **Keep for now; candidate.** Steps 1-4 could collapse to one sentence; no test pins them, but no incident justifies the churn and the prompt is cheap. Revisit with eval data. |
| F-13 | `fix_issue.py:921-933` (`render_e2e_prompt` step 3: "…until green or a reasonable attempt cap") | Loop bound | Vague bound, but advisory gate; no quality impact observed. | Low | **Keep.** |
| F-14 | `build.py:5074`, `fix_issue.py:788` ("Check architecture, security, performance, coverage, code quality") | Generic checklist | Current models review these dimensions unprompted; the line costs little and anchors the adversarial slot. | Low | **Keep.** |
| F-15 | `build.py:5792-5794` (envelope re-ask: "Do NOT redo the work or create new commits") | Scope fence | Protects committed work (R10). | High (keep) | **Keep.** |
| F-16 | `plugins/autonomous-sdlc/skills/*/SKILL.md` (8 skills, 705 lines) | Step lists | Every skill is either a thin wrapper shelling out to the `sdlc` controller (`build-stories`, `fix-issue`) or an interview/scaffold workflow (`brainstorm`, `create-epic`, `create-story`, `generate-epics`, `project-init`, `resume-build-agents`). No thinking-tag, reasoning, or tool-choice rules; numbered lists describe artifacts and ordering, not cognition. | High that nothing is obsolete | **Keep.** |

## Out of scope but noted

- `plugins/autonomous-sdlc/skills/_shared/bugfix-agent-prompt.md` (steps 1-5
  "Reproduce/Isolate/Inspect/…" and the six-step reception sequence) is the
  skill-side twin of F-9 and is covered by `test_shared_gate_prompts.py`; it
  follows the same disposition.
- `render_lens_prompt` (`overengineering.py`) and the doc-update prompt were not
  in the story scope.

## Follow-ups (not done here)

1. Re-run the Epic-26 pressure scenarios on the current models, then decide F-9
   and F-12 on evidence.
2. Track final-reply format compliance (F-1) per model in the eval harness.
