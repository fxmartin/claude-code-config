<!-- ABOUTME: RED/GREEN skill pressure-test harness for the Epic-26 discipline prompts (Story 26.3-001). -->
<!-- ABOUTME: On-demand, agent-driven; deliberately not wired into CI — CI integration is deferred to Epic-18. -->

# Skill pressure-tests (RED/GREEN)

Behavioral tests that prove the Epic-26 **discipline prompts actually change
agent behaviour under pressure** — not just that the prompt text exists (the
`controller/tests/test_*discipline*.py` / `test_finding_verification.py` suites
already assert the text). A discipline is only worth shipping if an agent
*without* it misbehaves and an agent *with* it complies; if you never watched it
fail, you don't know the skill teaches the right thing.

Pattern source: [obra/superpowers](https://github.com/obra/superpowers) (MIT,
Jesse Vincent) `skills/writing-skills` — the "TDD for skills" mapping table:
run the scenario **without** the skill and record the failure (RED), add the
skill, verify compliance (GREEN), then close the loopholes the agent finds.

## Runner: `claude plugin eval` (evaluated first, chosen over custom)

Story 26.3-001 required evaluating `claude plugin eval` *before* building
anything custom. We did, and it fits — nothing custom was built:

- It discovers cases at `evals/**/prompt.md` + `graders/*.md` (this layout).
- `--ablation with-without` runs each case **twice** — once with the plugin
  loaded (GREEN arm) and once without it (RED arm) — and reports the score
  delta. That ablation *is* the RED/GREEN split: the discipline ships inside
  this plugin, so "without the plugin" is "without the discipline".
- `--judge-model` (default `haiku`) scores each run against the case's
  `graders/criteria.md` rubric.
- `--max-cost-usd`, `--runs`, and per-case `--case`/`--tag` filters bound spend
  and nondeterminism.

> `claude plugin eval` is in early access. If it is unavailable, the suite is
> still runnable by hand (see "Running without the eval CLI" below) — the
> artifacts are plain markdown, not tied to any one runner.

### Run it (on-demand)

```bash
# From the repo root. Targets this plugin by name so the with/without-plugin
# ablation arm is added automatically.
claude plugin eval autonomous-sdlc --ablation with-without

# One case, cheap smoke:
claude plugin eval autonomous-sdlc --case 'root-cause-discipline' --runs 1

# CI-shaped JSON (for a future gate — see "CI stance"):
claude plugin eval autonomous-sdlc --json --max-cost-usd 2
```

A GREEN pass means the with-plugin arm meets the grader and beats the no-plugin
(RED) arm. Compare the live RED arm against the committed `baseline.md` record —
if the no-plugin arm has *stopped* misbehaving, the scenario has lost its
pressure and needs sharpening, not celebrating.

### Running without the eval CLI

Each case is self-contained markdown, so you can reproduce a run by hand:

1. **RED** — start an agent session **without** this plugin, paste the case's
   `prompt.md`, and confirm the behaviour matches `baseline.md`.
2. **GREEN** — repeat **with** the plugin enabled; grade the transcript against
   `graders/criteria.md`.

## CI stance — on-demand only, not a PR gate

These tests dispatch **live agents**: real cost, real nondeterminism. They are
**not** wired into the PR gate and must not be. What CI *does* protect is the
suite's *shape* — `controller/tests/test_skill_pressure_tests.py` asserts every
case keeps its scenario, its recorded baseline, and its grader, and still traces
to the discipline prompt it proves. Wiring the live runs into automation is
deferred to **Epic-18** (the eval-harness work): if Epic-18's scored harness
lands, rebuild these as eval cases inside it rather than standing up a second
scheduler. Epic-18 scores *output quality* on real tickets; this suite verifies
*process compliance* under pressure — complementary, not overlapping.

## Layout

```
evals/
├── README.md                         # this file — the reusable harness
├── root-cause-discipline/            # proves Story 26.1-001
│   ├── prompt.md                     # the pressure scenario (drives a symptom patch)
│   ├── baseline.md                   # recorded RED behaviour (the committed baseline)
│   └── graders/criteria.md           # GREEN compliance rubric (LLM judge)
└── finding-verification/             # proves Story 26.2-001
    ├── prompt.md                     # deliberately wrong review finding
    ├── baseline.md                   # recorded RED behaviour
    └── graders/criteria.md           # GREEN compliance rubric
```

## Add a new case in an hour, not a design session

The structure is deliberately a fixed triple. To pressure-test another skill:

1. `mkdir -p evals/<case-name>/graders`.
2. **`prompt.md`** — write the scenario. It must *actively pressure* the agent
   toward the failure the discipline prevents (deadline, low retry budget, an
   "obvious" shortcut, a plausible-but-wrong claim). Reference the discipline
   prompt under test by path so the case traces to what it proves. If the
   scenario needs files to act on, describe them inline or add a `scaffold`
   the agent can create — keep it small enough to fit one agent turn.
3. **`baseline.md`** — run the RED arm (no plugin) once and record what the
   un-disciplined agent actually did. This is the evidence the skill is needed;
   commit it. A case with no observed RED failure is not yet a test.
4. **`graders/criteria.md`** — write the GREEN rubric as concrete pass/fail
   criteria the judge can apply to a transcript. Reward the disciplined
   behaviour by name; reject the RED baseline behaviour explicitly.

That is the whole method: **scenario + baseline expectation + compliance
expectation**. Keep each file focused; the harness carries the rest.
