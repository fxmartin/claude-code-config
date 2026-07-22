# ABOUTME: Typer entry point for the `sdlc` controller CLI (Story 7.1-001).
# ABOUTME: Ships --version, --help, and every build/resume/observability verb.

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from sdlc import __version__
from sdlc.contracts import AGENT_SCHEMAS, ContractError, parse_and_validate
from sdlc.eval_compare import DEFAULT_TOLERANCE

# The full set of planned subcommands with one-line descriptions. `--help`
# renders these even while the bodies are stubs, so the surface area is visible
# from day one. Keep the keys in sync with the Epic-07 success metrics.
PLANNED_SUBCOMMANDS: dict[str, str] = {
    "build": "Run the full build-stories orchestration for a scope.",
    "fix": "Autonomously fix a single GitHub issue end-to-end (investigate → merge).",
    "resume": "Resume an interrupted build from the ledger state.",
    "status": "Show the current run status and stage progress.",
    "doctor": "Health-check the install, ledger, runs, config, and dependencies.",
    "runs": "List every known run from the host-level registry.",
    "state": "Inspect the persisted state machine for a run.",
    "validate": "Validate an agent response against its JSON schema.",
    "rollback": "Roll a run back to a prior ledger checkpoint.",
    "reconcile": "Re-check a run against origin/main and correct the ledger.",
    "usage-reconcile": "Backfill per-stage usage from the session logs and score agreement.",
    "model-backfill": "Backfill per-stage model attribution from the session logs.",
    "clean": "Garbage-collect build leftovers (orphan worktrees, merged branches, stale logs).",
    "sync-check": "Verify the Codex mirror's shared-skills submodule is in sync.",
    "generate-skills": "Generate Claude + Codex skill files from the neutral sources.",
    "repair": "Restore the framework's managed symlinks/config without a full reinstall.",
    "sast": "Classify a semgrep report into a CLEAN | WARN | BLOCK gate verdict.",
    "depscan": "Classify an osv-scanner report into a CLEAN | WARN | BLOCK gate verdict.",
    "supplychain": "Scan hooks/skills/MCP/settings for dangerous patterns (CLEAN | WARN | BLOCK).",
    "run-open": "Register a fix-issue run so the dashboard surfaces it.",
    "run-stage": "Log a fix-issue phase start/finish to the ledger.",
    "run-close": "Finalize a fix-issue run (DONE/FAILED) in ledger + registry.",
    "review-packet": "Bake the deterministic review packet (CR meta, files, diff) for a PR/MR.",
    "eval": "Run the agentic eval harness over a fixed ticket set and emit a scoreboard.",
    "eval-compare": "Compare two eval scoreboards (A/B) with a per-metric delta + verdict.",
    "eval-baseline": "Check an eval scoreboard against a committed baseline; flag regressions.",
}

app = typer.Typer(
    name="sdlc",
    help="External controller for the autonomous-SDLC state machine.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the controller version and exit.",
    ),
) -> None:
    """sdlc — deterministic controller for autonomous SDLC runs."""


# Note: there is no `init` verb. Epic-07 scaffolded one as a stub, but `build`
# already creates the SQLite ledger on first use (`Ledger.init()` runs inside
# `run_build`), so a separate workspace-scaffold command had no distinct job.
# Story 10.2-001 resolved it by removal — see docs/adr/001-controller-runtime.md.


_BUILD_EPILOG = """\
\b
Scope (positional, default `all`):
  all              every epic
  epic-NN          one epic by number, e.g. epic-34
  <name>           epic-name substring, e.g. user-management
  X.Y-NNN          a single story, e.g. 34.5-003
  epic-A epic-B    several scopes (space- or comma-separated, any order) build the
                   union of their incomplete stories in one run, e.g.
                   `epic-15 epic-18` or `epic-15,epic-18`; `all` mixed in wins

\b
Flags:
  --dry-run                 plan only; dispatch nothing
  --auto                    non-interactive run
  --skip-coverage           build agent opens the PR directly (no coverage gate)
  --skip-preflight          skip the preflight quality gate
  --rebuild                 rebuild stories the epic already marks Done
  --sequential              one story at a time (no cohort parallelism)
  --concurrency=N           max stories of a cohort to run at once in parallel
                            mode (default 5; --sequential forces 1)
  --limit=N                 build at most N stories
  --coverage-threshold=N    required new-code coverage % (default 90)
  --preflight-timeout=SEC   abort the preflight gate after SEC seconds (default 600)
  --budget=N                token ceiling for the run; a $-value (e.g. $5) is
                            converted to a notional API-equivalent token ceiling
  --budget-policy=POLICY    on crossing the ceiling: pause (resumable, default)
                            or abort (terminal stop)
  --rate-limit-max-wait=SEC in-process auto-wait cap for a Max rate-limit pause
                            (default 18000 ≈ 5h); a reset within it auto-resumes
                            the same run, beyond it parks RATE_LIMITED for resume
  --window-budget=N         configured per-window token budget (a $-value
                            converts to a notional ceiling); 0 = rely only on
                            live rate-limit signals (default)
  --window=SEC              rolling rate-limit window length (default 18000 ≈ 5h)
  --rate-limit-threshold=F  pause at this fraction of the window budget
                            (default 1.0; <1 pauses near the limit)
  --cost-threshold=N        per-stage pre-dispatch estimate ceiling; over it,
                            --auto warns and proceeds, interactive gates the
                            stage before spend. A $-value converts to a notional
                            token ceiling; 0 = estimate only, no gate (default)
  --model-routing=PROFILE   per-stage model map: balanced | quality-first |
                            quota-max | off (default off = CLI default for all
                            stages). Balanced cuts quota burn; the adversarial
                            skeptic keeps an Opus floor on high-risk / large
                            stories and tiers down to Sonnet on low-risk ones
  --model-<stage>=MODEL     pin one stage's model, winning over the map (escape
                            hatch), e.g. --model-build=opus --model-merge=haiku
  --harness ROLE=NAME,...   route pipeline roles to harnesses from the registry
                            (controller/config/harnesses.yaml), e.g.
                            --harness build=claude,review=codex,qa=codex. Roles:
                            build, coverage (qa), review, merge, docs. Unmapped
                            roles run on the default (claude). An unknown/disabled
                            harness fails fast in preflight. A repo can set its own
                            default in a root .sdlc-harness.yaml (the flag wins);
                            see docs/harness-adapters.md
  --thinking-cap=N          cap per-request thinking tokens (MAX_THINKING_TOKENS)
                            on every dispatched agent; 0 = no cap (default)
  --sandbox                 run every dispatched agent inside a no-egress,
                            cap-dropped, non-root container with the worktree
                            mounted (recommended for untrusted repos); fails fast
                            if no container runtime is present. SDLC_SANDBOX=1 is
                            the per-repo config equivalent
"""


@app.command(
    help=PLANNED_SUBCOMMANDS["build"],
    epilog=_BUILD_EPILOG,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def build(ctx: typer.Context) -> None:
    """Run the full build-stories orchestration for a scope.

    Scope is ``all``, ``epic-NN``, an epic name, or a single story ``X.Y-NNN``.
    Flags: ``--dry-run --auto --skip-coverage --skip-preflight --rebuild
    --sequential --limit=N --coverage-threshold=N --preflight-timeout=SEC`` (see
    the epilog in ``build --help``). The controller owns the state machine;
    agents are dispatched as subprocesses and every response is schema-validated
    before the next stage runs.
    """
    from sdlc.build import parse_build_args, run_build
    from sdlc.discovery import discover_queue
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view
    from sdlc.registry import Registry

    try:
        opts = parse_build_args(ctx.args)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Story 20.7-005: merge a repo-root `.sdlc-harness.yaml` under the CLI
    # `--harness` map (precedence: CLI flag > repo file > built-in default) so the
    # preflight below and every stage dispatch see one effective role->harness map.
    # A malformed file or an unknown role fails fast here, the same path as the flag.
    from sdlc.harness import HarnessError
    from sdlc.role_routing import (
        RoleRoutingError,
        apply_registry_default,
        apply_repo_harness_defaults,
        default_registry_path,
        registry_default_harness,
    )

    try:
        opts.harness_map = apply_repo_harness_defaults(opts.harness_map)
        # The harness registry's top-level `default:` is the global default
        # harness: it routes every role left unmapped by `--harness` and the repo
        # `.sdlc-harness.yaml`. Precedence: --harness flag > repo file > registry
        # default > built-in claude. A registry default of `claude` (or a missing
        # registry) is a no-op, so the empty-map fast path below still skips
        # routing and behaviour is byte-identical to today.
        reg_default = registry_default_harness(default_registry_path())
        opts.harness_map = apply_registry_default(opts.harness_map, reg_default)
    except (RoleRoutingError, HarnessError) as exc:
        # registry_default_harness delegates registry validation to the harness
        # loader, which raises HarnessError (not RoleRoutingError) on a malformed
        # registry or a `default:` naming an undefined harness — the most likely
        # typo on the new toggle. Catch it here so it exits cleanly (2) like every
        # other config error rather than surfacing a traceback.
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Story 20.2-001: resolve and validate per-role harness routing before any
    # stage runs. An unknown/disabled harness, a missing registry, or a conflict
    # with the adversarial-reviewers registry fails fast here (no half-run). With
    # no `--harness` map (and no repo file) this is skipped entirely — behaviour is
    # unchanged.
    if opts.harness_map:
        from sdlc.role_routing import (
            check_review_bridge,
            default_registry_path,
            default_reviewers_path,
            reconcile_reviewer_registry,
            resolve_role_routing,
        )

        try:
            resolved_harnesses = resolve_role_routing(
                opts.harness_map, config_path=default_registry_path()
            )
            check_review_bridge(
                resolved_harnesses, reviewers_path=default_reviewers_path()
            )
            # Story 20.3-002: the reviewer registry is a view over the harness
            # registry — fail fast if a Codex reviewer link has diverged from its
            # harness (dangling link, or enabled-reviewer/disabled-harness).
            reconcile_reviewer_registry(
                registry_path=default_registry_path(),
                reviewers_path=default_reviewers_path(),
            )
        except RoleRoutingError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    queue = discover_queue(opts.scope)

    # A targeted scope that matches nothing is almost always a typo or an
    # unsupported form — fail loudly rather than reporting a hollow success.
    # (`all` legitimately yields an empty queue in a fresh repo; leave it be.)
    if opts.scope.lower() != "all" and not queue:
        typer.echo(
            f"error: scope '{opts.scope}' matched no stories — check the story id "
            "or epic name/number.",
            err=True,
        )
        raise typer.Exit(code=2)

    ledger = Ledger(default_db_path())
    # Bring a pre-existing ledger up to the current schema before the build
    # reads or writes it (run_build's init() also migrates, but a stale DB must
    # be safe the moment any code touches it). No-op when no DB exists yet.
    ledger.ensure_migrated()
    # Story 22.5-001 AC1: resolve the host adapter once so run_build can stamp the
    # run's actor from host identity. Best-effort — a repo with no/unsupported
    # host remote yields no adapter, and run_build degrades the actor to
    # `unknown` (it never blocks a build; AC3).
    from sdlc.issue_host import IssueHostError, get_adapter, resolve_host

    try:
        actor_adapter = get_adapter(resolve_host(Path.cwd()))
    except IssueHostError:
        actor_adapter = None
    result = run_build(
        opts,
        queue=queue,
        ledger=ledger,
        render_view=make_render_view(ledger.db_path),
        registry=Registry(),
        actor_adapter=actor_adapter,
    )

    if result.skipped_in_test:
        # Story 12.1-002: the recursion guard fired (SDLC_IN_TEST set) — a project
        # test invoked `sdlc build` bare during preflight. Report and exit cleanly
        # so the parent suite does not recurse into pytest-within-pytest.
        from sdlc.build import IN_TEST_ENV_VAR

        typer.echo(
            f"{IN_TEST_ENV_VAR} is set — skipping real build orchestration "
            "(recursion guard, Story 12.1-002)."
        )
        raise typer.Exit(code=0)

    if result.preflight_failed:
        typer.echo(
            "PRE_FLIGHT_FAILURE: test suite is red on main — fix before building.",
            err=True,
        )
        raise typer.Exit(code=1)

    if result.dry_run:
        typer.echo(f"dry run: {result.planned} stories queued (not building).")
        raise typer.Exit(code=0)

    typer.echo(
        f"build finished: {result.completed} done, {result.failed} failed, "
        f"{result.blocked} blocked, {result.needs_attention} need attention, "
        f"{result.awaiting_approval} awaiting approval, {result.skipped} skipped."
    )
    # Story 14.1-001: a budget-gated stop is reported with the labelled-notional
    # dollar figure so the $ is never mistaken for real subscription spend. Pause
    # leaves the run resumable; abort is a terminal stop.
    if result.budget_stopped:
        from sdlc.build import notional_cost_label

        tail = (
            "run paused — raise --budget and `sdlc resume` to continue."
            if result.budget_policy == "pause"
            else "run aborted."
        )
        typer.echo(
            f"budget ceiling crossed: {result.accrued_tokens} tokens accrued "
            f"(ceiling {opts.budget}); "
            f"{notional_cost_label(result.notional_cost_usd)} — {tail}"
        )
    # Story 14.1-003: a rate-limit park (reset beyond the auto-wait cap) leaves
    # the run RATE_LIMITED (resumable). Report it as a time-based pause, not a
    # failure, so a wrapper knows `sdlc resume` will continue it once the window
    # reopens. (Within-cap auto-waits never reach here — they resume in-process.)
    if result.rate_limited:
        waited = (
            f" (auto-waited {result.rate_limit_waited_s}s first)"
            if result.rate_limit_waited_s
            else ""
        )
        typer.echo(
            f"rate limit reached{waited} — run parked RATE_LIMITED; `sdlc resume` "
            "continues it once the Max plan's window reopens."
        )
    # Story 14.1-002: the interactive cost gate paused the run (resumable). Report
    # it so a wrapper knows to raise --cost-threshold and resume.
    if result.cost_gated:
        typer.echo(
            "cost gate reached — run paused IN_PROGRESS; raise --cost-threshold "
            "and `sdlc resume` to continue the gated stage."
        )
    # An AWAITING_APPROVAL run is honestly not a failure (Story 12.3-003), but it
    # still needs FX to act, so it is not "clean" — exit non-zero like
    # NEEDS_ATTENTION so a wrapping script never reads it as fully done. A
    # budget-stopped, rate-limited, or cost-gated run is likewise not fully done.
    clean = (
        result.failed == 0
        and result.blocked == 0
        and result.needs_attention == 0
        and result.awaiting_approval == 0
        and not result.budget_stopped
        and not result.rate_limited
        and not result.cost_gated
    )
    raise typer.Exit(code=0 if clean else 1)


_FIX_EPILOG = """\
\b
Target (positional — pass exactly one):
  <issue-number>   a single open GitHub issue, e.g. `sdlc fix 123`
  all              every open issue (bugs first, then enhancements by priority)
  next             the highest-priority open bug (see --limit for the top N)

\b
Flags:
  --limit=N                 batch only: cap the issue set (`next` defaults to 1)
  --sequential              batch only: one issue fully completes before the next
  --concurrency=N           batch only: issue-level worker cap (default 5)
  --skip-coverage           build agent opens the PR directly (no coverage gate)
  --coverage-threshold=N    required new-code coverage % (default 90)
  --skip-preflight          skip the preflight quality gate
  --e2e-gate=warn|off       run the advisory E2E gate after review (default off)
  --skip-e2e                alias for --e2e-gate=off

\b
Batch runs investigate every issue first, then serialize only issues that touch
overlapping files while independent ones run concurrently. The run appears in
`sdlc dashboard` beside `sdlc build` runs.
"""


@app.command(
    help=PLANNED_SUBCOMMANDS["fix"],
    epilog=_FIX_EPILOG,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def fix(ctx: typer.Context) -> None:
    """Autonomously fix one or many GitHub issues end-to-end.

    Fetches the issue(s), investigates the root cause, then drives the reused
    build → coverage → review → merge pipeline (with the bounded bugfix loop and
    high-risk merge parking) before a best-effort summary — mirroring the
    fix-issue skill in the controller (issue #436). ``all`` / ``next`` fan the
    pipeline out across many issues, serializing only those with overlapping files.
    """
    from sdlc.fix_issue import (
        FixBatchOptions,
        FixConfigError,
        parse_fix_args,
        run_fix,
        run_fix_batch,
    )
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view

    try:
        opts = parse_fix_args(ctx.args)
    except FixConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    ledger = Ledger(default_db_path())
    ledger.ensure_migrated()

    if isinstance(opts, FixBatchOptions):
        # _run_fix_batch_cli always raises typer.Exit — it never returns here.
        _run_fix_batch_cli(opts, ledger, run_fix_batch, make_render_view)

    result = run_fix(
        opts, ledger=ledger, render_view=make_render_view(ledger.db_path)
    )

    if result.preflight_failed:
        typer.echo(
            "PRE_FLIGHT_FAILURE: test suite is red on main — fix before running `sdlc fix`.",
            err=True,
        )
        raise typer.Exit(code=1)

    if result.aborted:
        typer.echo(f"fix aborted for issue #{result.issue}: {result.abort_reason}")
        raise typer.Exit(code=1)

    if result.investigation_blocked:
        typer.echo(
            f"fix blocked for issue #{result.issue}: investigation needs a human "
            f"decision ({result.block_reason})."
        )
        raise typer.Exit(code=1)

    pr = f" (PR #{result.pr_number})" if result.pr_number else ""
    typer.echo(f"fix finished for issue #{result.issue}: {result.status}{pr}.")
    raise typer.Exit(code=0 if result.status == "DONE" else 1)


def _run_fix_batch_cli(opts, ledger, run_fix_batch, make_render_view) -> None:
    """Drive a batch fix run and translate its outcome into output + an exit code.

    Exit 0 only when every issue landed cleanly (run terminal DONE and nothing
    failed); any failed/blocked/parked issue, a preflight failure, or a batch that
    could not even select issues exits non-zero.
    """
    result = run_fix_batch(
        opts, ledger=ledger, render_view=make_render_view(ledger.db_path)
    )

    if result.preflight_failed:
        typer.echo(
            "PRE_FLIGHT_FAILURE: test suite is red on main — fix before running `sdlc fix`.",
            err=True,
        )
        raise typer.Exit(code=1)

    if result.no_issues:
        typer.echo(result.summary or "no issues matched the batch target.")
        raise typer.Exit(code=0 if result.status == "DONE" else 1)

    typer.echo(result.summary)
    clean = result.status == "DONE" and result.failed == 0
    raise typer.Exit(code=0 if clean else 1)


@app.command(help=PLANNED_SUBCOMMANDS["resume"])
def resume(
    scope: list[str] | None = typer.Argument(
        None,
        help="Scope(s) of the run to resume: all, epic-NN, an epic name, or "
        "X.Y-NNN. Several scopes (space- or comma-separated, any order) resume a "
        "composite run; default all.",
    ),
    run: str | None = typer.Option(
        None,
        "--run",
        help="Resume a specific run id (default: the latest incomplete run).",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    budget: str | None = typer.Option(
        None,
        "--budget",
        help="Raise/override the run's token budget (a $-value converts to a "
        "notional API-equivalent ceiling). Required to continue a budget-paused run.",
    ),
    budget_policy: str | None = typer.Option(
        None,
        "--budget-policy",
        help="Override the budget policy: pause or abort.",
    ),
    cost_threshold: str | None = typer.Option(
        None,
        "--cost-threshold",
        help="Raise/override the per-stage cost-estimate gate (a $-value converts "
        "to a notional token ceiling; 0 disables it). Pass this to continue a "
        "story the gate parked.",
    ),
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        help="Override the cohort worker cap for this resume (>= 1; default: the "
        "value the original run used). A serial-mode run stays one-at-a-time.",
        min=1,
    ),
) -> None:
    """Resume an interrupted build from the SQLite ledger.

    Finds the most recent incomplete run for ``scope`` (a run still marked
    IN_PROGRESS because it never reached a clean close-out), recomputes the
    remaining queue from the markdown epics, and re-enters the 4-stage loop at
    the exact stage each story was interrupted in — branch, PR number, and
    attempt count preserved. Completed stories are not rebuilt. A run with no
    incomplete stories is a no-op that reports "nothing to resume" and exits 0.

    Story 14.1-001: a budget-paused run carries its token ceiling, so resuming
    without raising it re-pauses immediately. Pass ``--budget`` to raise it and
    continue.
    """
    from sdlc.build import _parse_budget_value
    from sdlc.discovery import canonical_scope
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view
    from sdlc.resume import run_resume

    # Story 19.1-001: fold the (possibly several) positional scopes into one
    # canonical label so a composite run resumes in any order; no positional
    # defaults to `all`, exactly as before.
    scope_label = canonical_scope(scope or ["all"])

    budget_tokens: int | None = None
    if budget is not None:
        try:
            budget_tokens, _ = _parse_budget_value(budget)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    if budget_policy is not None and budget_policy not in {"pause", "abort"}:
        typer.echo(
            f"error: invalid --budget-policy: {budget_policy} (expected pause|abort)",
            err=True,
        )
        raise typer.Exit(code=2)
    cost_threshold_tokens: int | None = None
    if cost_threshold is not None:
        try:
            cost_threshold_tokens, _ = _parse_budget_value(cost_threshold)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before resume reads it.
    ledger.ensure_migrated()
    result = run_resume(
        scope_label,
        ledger=ledger,
        run_id=run,
        render_view=make_render_view(db_path),
        budget=budget_tokens,
        budget_policy=budget_policy,
        cost_threshold=cost_threshold_tokens,
        concurrency=concurrency,
    )

    if result.nothing_to_resume:
        if result.run_id is None:
            typer.echo(f"nothing to resume: no incomplete run for scope '{scope_label}'.")
        else:
            typer.echo(
                f"nothing to resume: run {result.run_id[:8]} has no incomplete stories."
            )
        raise typer.Exit(code=0)

    typer.echo(
        f"resume finished: {result.completed} done, {result.failed} failed, "
        f"{result.blocked} blocked, {result.needs_attention} need attention, "
        f"{result.awaiting_approval} awaiting approval ({result.resumed} resumed)."
    )
    if result.budget_stopped:
        from sdlc.build import notional_cost_label

        tail = (
            "run paused — raise --budget further and `sdlc resume` to continue."
            if result.budget_policy == "pause"
            else "run aborted."
        )
        typer.echo(
            f"budget ceiling crossed: {result.accrued_tokens} tokens accrued; "
            f"{notional_cost_label(result.notional_cost_usd)} — {tail}"
        )
    # Story 14.1-003: a resume that re-hit the window beyond the cap re-parks.
    if result.rate_limited:
        typer.echo(
            "rate limit still in effect — run re-parked RATE_LIMITED; `sdlc resume` "
            "again once the Max plan's window reopens."
        )
    # Story 14.1-002: an un-raised resume re-trips the cost gate (still IN_PROGRESS).
    if result.cost_gated:
        typer.echo(
            "cost gate still in effect — run left IN_PROGRESS; raise "
            "--cost-threshold further and `sdlc resume` to continue."
        )
    clean = (
        result.failed == 0
        and result.blocked == 0
        and result.needs_attention == 0
        and result.awaiting_approval == 0
        and not result.budget_stopped
        and not result.rate_limited
        and not result.cost_gated
    )
    raise typer.Exit(code=0 if clean else 1)


@app.command(help=PLANNED_SUBCOMMANDS["status"])
def status(
    run: str | None = typer.Option(
        None, "--run", help="Run id to inspect (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON snapshot."
    ),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Emit a portable, secret-free markdown handoff (readiness + run + approvals).",
    ),
    write: Path | None = typer.Option(
        None,
        "--write",
        help="With --markdown, write the report to this file instead of stdout.",
    ),
) -> None:
    """Show the progress of a build run from the SQLite ledger.

    Reads the ledger **read-only** (safe to poll while a build is writing) and
    prints a run summary, a per-story table, and the most recent events. With
    ``--json`` it emits one object so the build-stories skill can poll it and
    report progress. With ``--markdown`` (optionally ``--write <file>``) it emits
    a portable handoff a colleague can paste into an issue or chat. When there is
    no ledger or no run yet it says so and exits 0 — absence means "not started",
    not an error.
    """
    from sdlc.build import Ledger, status_snapshot
    from sdlc.ledger_view import default_db_path

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Apply any pending migrations (writable connection) before the read-only
    # snapshot, so a stale ledger never crashes the query with "no such column".
    # No-op when the DB does not exist — absence stays "not started".
    ledger.ensure_migrated()
    snap = status_snapshot(ledger, run)

    # The markdown handoff (Story 15.1-002) renders even with no run — readiness
    # and "no active run" are exactly what a colleague needs to share.
    if markdown:
        from sdlc.doctor import run_doctor
        from sdlc.status import format_markdown

        report = run_doctor(db_path=db_path)
        md = format_markdown(snap, report.to_dict())
        if write is not None:
            write.write_text(md, encoding="utf-8")
            typer.echo(f"wrote status handoff to {write}")
        else:
            typer.echo(md, nl=False)
        raise typer.Exit(code=0)

    if snap["run"] is None:
        if as_json:
            typer.echo(json.dumps(snap, default=str))
        else:
            typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    if as_json:
        typer.echo(json.dumps(snap, default=str))
        raise typer.Exit(code=0)

    # Human-readable snapshot.
    run_id = snap["run"]["id"]
    counts = snap["counts"]
    stories = snap["stories"]
    events = snap["events"]
    # Story 17.3-001: surface the run's real concurrency — "N/M workers busy" —
    # so a parallel run shows several stories active at once, not just one. Only
    # a genuine parallel run (cap > 1) carries the figure; a serial run is silent.
    concurrency = snap["run"].get("concurrency") or {}
    worker_limit = concurrency.get("limit") or 1
    workers = (
        f", {concurrency.get('active', 0)}/{worker_limit} workers busy"
        if worker_limit > 1
        else ""
    )
    typer.echo(
        f"run {run_id[:8]}  {snap['run'].get('status', '?')}  "
        f"{counts['done']}/{counts['total']} done, {counts['failed']} failed, "
        f"{counts['blocked']} blocked, {counts['in_progress']} in progress  "
        f"(scope={snap['run'].get('scope', '?')}, {snap['run'].get('mode', '?')}{workers})"
    )
    # Story 27.3-004: rate-limit stall time, kept apart from stage durations so
    # quota backoff is diagnosable at a glance. Silent when the run never stalled.
    stall_s = snap["run"].get("stall_seconds") or 0
    if stall_s:
        typer.echo(
            f"rate-limit stalls: {stall_s}s waited (not counted as agent runtime)"
        )
    if stories:
        # Story 27.2-002 AC4: MODEL shows the tier(s) the story's stages ran on
        # (first-use order); "-" when routing was off (CLI default everywhere).
        typer.echo(f"  {'STORY':<14}{'STATUS':<13}{'STAGE':<11}{'PR':<7}MODEL")
        for s in stories:
            stage = s.get("current_stage") or "-"
            pr = s.get("pr_number")
            pr_disp = f"#{pr}" if pr else "-"
            models_disp = ",".join(s.get("models") or []) or "-"
            typer.echo(
                f"  {str(s.get('story_id', '?')):<14}"
                f"{str(s.get('status', '?')):<13}{str(stage):<11}"
                f"{pr_disp:<7}{models_disp}"
            )
            # Sub-stage activity for an in-flight story (Story 11.1-002): the
            # latest progress milestone, e.g. "↳ build: editing cli.py". Absent
            # for finished stories or runs without streamed progress.
            activity = s.get("activity")
            if s.get("status") == "IN_PROGRESS" and activity:
                msg = activity.get("message") or activity.get("kind") or ""
                act_stage = activity.get("stage") or stage
                if msg:
                    typer.echo(f"    ↳ {act_stage}: {msg}")
            # Per-story stall time (Story 27.3-004): only stories that actually
            # waited on a rate limit carry the line.
            if s.get("stall_seconds"):
                typer.echo(f"    ⏸ stalled {s['stall_seconds']}s on rate limits")
    if events:
        typer.echo("recent:")
        for e in events:
            typer.echo(
                f"  {e.get('ts', '')}  {str(e.get('level', '')):<8}"
                f"{str(e.get('source') or ''):<11} {e.get('message', '')}"
            )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["doctor"])
def doctor(
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    claude_dir: Path | None = typer.Option(
        None,
        "--claude-dir",
        help="Install dir to check (default: ~/.claude).",
    ),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Config repo root for config checks (default: the git toplevel).",
    ),
    exit_code: bool = typer.Option(
        False,
        "--exit-code",
        help="Exit non-zero when any check is WARN (1) or FAIL (2), for automation.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the report as a JSON object."
    ),
    gitlab: bool = typer.Option(
        False,
        "--gitlab",
        help="Also run the GitLab adoption preflight (glab auth, project, CI, gate template).",
    ),
    target: Path | None = typer.Option(
        None,
        "--target",
        help="Target repo root for the GitLab preflight (default: cwd).",
    ),
) -> None:
    """Check the install and run state, reporting a remedy for each problem.

    Runs read-only health-checks across install integrity (managed symlinks),
    ledger schema currency + integrity, stuck/stale runs (an IN_PROGRESS run with
    a dead pid or no recent activity), config validity (settings/schemas parse),
    ledger-vs-logs usage agreement, per-stage model attribution, and dependency
    availability (gh, claude, semgrep, osv-scanner). Each finding reports
    CLEAN/WARN/FAIL plus the command or doc that fixes it.

    The **model attribution** check (Story 28.1-002) reports the share of
    dispatched stage attempts across the same window carrying a non-NULL
    `stages.model` — the column cost-by-model reads. A completed stage on the
    *latest* run whose own transcript names a model it did not record FAILs (the
    live recording had it and dropped it); older, non-DONE, or unrecoverable
    NULLs WARN and are fixable with `sdlc model-backfill --all` where the
    transcript survives. Rows whose model is genuinely unrecoverable are counted
    as such, never coerced to a placeholder.

    The **usage agreement** check (Story 28.1-001) scores the share of verifiable
    stage attempts across the last 5 runs whose recorded token/cost usage matches
    its session log's ground truth, and enumerates the residual disagreements with
    their reason — `still-divergent` (WARN; run `sdlc usage-reconcile --all` to
    backfill), `log-recovered` (a crashed session: tokens recovered, cost genuinely
    unavailable), or `no-log`/`no-usage` (the transcript was pruned or carried no
    usage — reported as *unverifiable* and excluded from the rate, never counted
    as agreement).

    ``--gitlab`` additionally runs the GitLab adoption preflight against
    ``--target`` (default: cwd): glab installed/authenticated, the project +
    default branch exist, CI is enabled, and the `.gitlab-ci.yml` gate template is
    present (Story 23.6-002). See docs/gitlab-adoption.md for the worked example.

    Always exits 0 by default so it is safe to run anywhere; ``--exit-code`` makes
    a WARN exit 1 and a FAIL exit 2 so a wrapping script can gate on health.
    """
    from sdlc.doctor import run_doctor

    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=db,
    )

    if gitlab:
        from sdlc.gitlab_preflight import run_gitlab_preflight

        preflight = run_gitlab_preflight(repo_root=target or Path.cwd())
        report.findings.extend(preflight.findings)

    if as_json:
        typer.echo(json.dumps(report.to_dict(), default=str))
        raise typer.Exit(code=0)

    for finding in report.findings:
        typer.echo(f"[{finding.status}] {finding.name} — {finding.detail}")
        if finding.remedy:
            typer.echo(f"    ↳ remedy: {finding.remedy}")
    if report.status == "CLEAN":
        typer.echo(f"doctor: all {len(report.findings)} checks passed (CLEAN).")
    else:
        typer.echo(f"doctor: overall {report.status} — see remedies above.")

    if exit_code and report.status != "CLEAN":
        raise typer.Exit(code=2 if report.status == "FAIL" else 1)
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["runs"])
def runs(
    as_json: bool = typer.Option(
        False, "--json", help="Emit the registry view as a JSON array."
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Remove crashed (dead-pid) entries before listing."
    ),
) -> None:
    """List every known run across repos from the host-level registry.

    The registry is a discovery cache written by each ``sdlc build`` (default
    ``~/.sdlc/registry.json``, XDG-aware). A run whose process has died without a
    clean finish surfaces as ``DEAD`` rather than lingering as in-progress;
    ``--prune`` drops those entries. Each repo's own ledger stays authoritative
    for run detail. Exits 0 even when empty — no runs means "nothing started".
    """
    from sdlc.registry import Registry

    registry = Registry()
    if prune:
        removed = registry.prune()
        if not as_json:
            typer.echo(f"pruned {removed} dead run(s).")

    rows = registry.view()
    if as_json:
        typer.echo(json.dumps(rows, default=str))
        raise typer.Exit(code=0)

    if not rows:
        typer.echo("no runs registered.")
        raise typer.Exit(code=0)

    typer.echo(f"{'RUN':<14}{'SCOPE':<14}{'STATE':<14}{'PROGRESS':<10}REPO")
    for r in rows:
        run_disp = str(r.get("run_id", "?"))[:12]
        total = r.get("total")
        completed = r.get("completed")
        progress = f"{completed}/{total}" if total is not None else "-"
        typer.echo(
            f"{run_disp:<14}{str(r.get('scope', '?')):<14}"
            f"{str(r.get('state', '?')):<14}{progress:<10}{r.get('repo', '?')}"
        )
    raise typer.Exit(code=0)


@app.command(name="run-open", help=PLANNED_SUBCOMMANDS["run-open"])
def run_open_cmd(
    scope: str = typer.Option(
        ..., "--scope", help="The issue being fixed (e.g. issue-42); the run's scope."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo path recorded in the registry (default: cwd)."
    ),
    mode: str = typer.Option(
        "fix-issue", "--mode", help="Run lineage tag (default: fix-issue)."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Synthetic story id (default: the scope)."
    ),
    title: str | None = typer.Option(
        None, "--title", help="Human title for the run's story (default: the scope)."
    ),
    pid: int | None = typer.Option(
        None,
        "--pid",
        help="Orchestrator pid whose liveness stands in for the run's (a markdown "
        "skill passes its $PPID, not this subprocess's pid). Default: this process.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit {run_id, db, story_id} as JSON instead of the id."
    ),
) -> None:
    """Open a fix-issue run in the ledger + host registry.

    Minimal run-logging verb the markdown ``fix-issue`` skill shells out to so its
    session shows up in the multi-run dashboard beside ``sdlc build`` runs. Prints
    the new run id on stdout for the skill to capture; exits non-zero when logging
    is unavailable so the skill's best-effort guard (``|| true``) can carry on.

    Pass ``--pid`` the *orchestrator's* long-lived pid (the skill's ``$PPID``): the
    ``sdlc run-open`` process itself exits immediately, so registering its own pid
    would make the registry derive a still-running fix ``DEAD``.
    """
    from sdlc.runlog import run_open

    handle = run_open(
        scope=scope,
        db=db,
        repo=repo,
        mode=mode,
        story_id=story_id,
        title=title,
        pid=pid,
    )
    if handle is None:
        typer.echo("run-open: logging unavailable (fix continues unlogged).", err=True)
        raise typer.Exit(code=1)
    if as_json:
        typer.echo(
            json.dumps(
                {"run_id": handle.run_id, "db": handle.db, "story_id": handle.story_id}
            )
        )
    else:
        typer.echo(handle.run_id)
    raise typer.Exit(code=0)


@app.command(name="run-stage", help=PLANNED_SUBCOMMANDS["run-stage"])
def run_stage_cmd(
    action: str = typer.Argument(..., help="start | finish"),
    run: str = typer.Option(..., "--run", help="Run id from run-open."),
    stage: str = typer.Option(
        ..., "--stage", help="Phase name (investigate/build/coverage/review/e2e/merge)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Story id (default: the run's sole story)."
    ),
    attempt: int = typer.Option(1, "--attempt", help="Attempt number (default: 1)."),
    status: str = typer.Option(
        "DONE", "--status", help="Terminal status for `finish` (default: DONE)."
    ),
    failure_category: str = typer.Option(
        "", "--failure-category", help="Optional failure category for `finish`."
    ),
) -> None:
    """Log a fix-issue phase boundary to the ledger (best-effort).

    ``start`` appends an IN_PROGRESS stage row; ``finish`` transitions it to
    ``--status``. Exits 2 on a bad action and 1 when the write failed, so the
    skill can ignore logging failures without aborting the fix.
    """
    if action not in ("start", "finish"):
        typer.echo("run-stage: action must be 'start' or 'finish'.", err=True)
        raise typer.Exit(code=2)

    from sdlc.runlog import run_stage

    ok = run_stage(
        action=action,
        run_id=run,
        stage=stage,
        db=db,
        story_id=story_id,
        attempt=attempt,
        status=status,
        failure_category=failure_category,
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command(name="run-close", help=PLANNED_SUBCOMMANDS["run-close"])
def run_close_cmd(
    run: str = typer.Option(..., "--run", help="Run id from run-open."),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    status: str = typer.Option(
        "DONE", "--status", help="Terminal run status (DONE/FAILED/ABORTED)."
    ),
    completed: int | None = typer.Option(
        None, "--completed", help="Completed count recorded on the run + registry."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Story id (default: the run's sole story)."
    ),
) -> None:
    """Finalize a fix-issue run terminal in the ledger and the registry.

    Stamps the run (and its sole story) ``--status`` and mirrors it into the
    registry so a clean finish stops deriving as DEAD. Exits 1 when the ledger
    write failed; the registry mirror is best-effort.
    """
    from sdlc.runlog import run_close

    ok = run_close(
        run_id=run, db=db, status=status, completed=completed, story_id=story_id
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command(help="Serve a local web dashboard of live build progress.")
def dashboard(
    run: str | None = typer.Option(
        None, "--run", help="Run id to show (default: always the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    port: int = typer.Option(8787, "--port", help="Port to bind (default: 8787)."),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Host to bind (default: localhost-only)."
    ),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the dashboard in a browser on start."
    ),
    stop: bool = typer.Option(
        False, "--stop", help="Stop a dashboard running on this host:port and exit."
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Stop any running dashboard on this host:port, then start fresh.",
    ),
) -> None:
    """Serve an auto-refreshing local dashboard of build progress.

    Reads the SQLite ledger read-only and serves it at ``http://host:port`` —
    progress display runs independently of any agent/turn loop, so it stays live
    for the whole build. Runs until Ctrl-C. Binds localhost only by default.

    Use ``--stop`` to stop a (possibly backgrounded) dashboard on this port, or
    ``--restart`` to replace it — handy after upgrading the controller.

    With no ``--db`` the dashboard discovers every run across repos from the
    host-level registry; pass ``--db <path>`` for the single-repo view.
    """
    from sdlc.build import in_test_sentinel
    from sdlc.dashboard import serve, stop_dashboard

    if stop or restart:
        n = stop_dashboard(host, port)
        typer.echo(
            f"stopped {n} dashboard process(es) on {host}:{port}."
            if n
            else f"no dashboard running on {host}:{port}."
        )
        if stop:
            raise typer.Exit(code=0)

    # Story 12.1-002: never bind a server when running inside another build's
    # preflight test suite (sentinel set) — a project test that invokes `sdlc
    # dashboard` bare would otherwise block the run on a socket. Placed after the
    # --stop/--restart handling (which only kills a server, never binds one) so
    # that legitimate coverage of those paths still runs under the sentinel (AC3).
    if in_test_sentinel():
        from sdlc.build import IN_TEST_ENV_VAR

        typer.echo(
            f"{IN_TEST_ENV_VAR} is set — skipping dashboard server "
            "(recursion guard, Story 12.1-002)."
        )
        raise typer.Exit(code=0)

    try:
        # db is None → registry-discovery mode (multi-run overview, Story 11.2-002).
        serve(db, host=host, port=port, run_id=run, open_browser=open_browser)
    except OSError as exc:
        # Almost always "address already in use" — a dashboard is likely running.
        typer.echo(
            f"could not bind {host}:{port} ({exc}). A dashboard is probably already "
            f"running at http://{host}:{port} — use `sdlc dashboard --restart` "
            f"to replace it, or `--stop` to stop it.",
            err=True,
        )
        raise typer.Exit(code=0) from exc


@app.command(help=PLANNED_SUBCOMMANDS["state"])
def state(
    run: str | None = typer.Option(
        None, "--run", help="Run id to dump (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the stage rows as a JSON array."
    ),
) -> None:
    """Dump the persisted state-machine rows for a run, for debugging.

    Reads the ledger **read-only** and prints one line per stage attempt
    (story id, stage, status, attempt, PR, branch) in a stable, greppable
    format. With ``--json`` it emits the same rows as an array. When there is no
    run it says so and exits 0.
    """
    from sdlc.build import Ledger
    from sdlc.ledger_view import default_db_path
    from sdlc.status import format_state, state_report

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before the read-only dump.
    ledger.ensure_migrated()
    rid = run or ledger.latest_run_id()

    if rid is None:
        if as_json:
            typer.echo(json.dumps([]))
        else:
            typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    rows = state_report(ledger, rid)
    if as_json:
        typer.echo(json.dumps(rows, default=str))
        raise typer.Exit(code=0)

    typer.echo(f"state for run {rid[:8]} ({len(rows)} stage rows)")
    for line in format_state(rows):
        typer.echo(line)
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["validate"])
def validate(
    agent_type: str = typer.Argument(
        ...,
        help=f"Agent type to validate. One of: {', '.join(sorted(AGENT_SCHEMAS))}.",
    ),
    response_file: Path | None = typer.Argument(
        None,
        help="File containing the agent response. Reads stdin when omitted.",
    ),
) -> None:
    """Validate an agent response against its JSON-schema contract.

    Reads the agent's free-form response (from a file or stdin), extracts the
    fenced result block, validates it against the schema for ``agent_type``,
    and prints the validated JSON. Validation failures exit non-zero with an
    actionable message naming the offending field.
    """
    if agent_type not in AGENT_SCHEMAS:
        valid = ", ".join(sorted(AGENT_SCHEMAS))
        typer.echo(
            f"error: unknown agent type {agent_type!r}; expected one of: {valid}",
            err=True,
        )
        raise typer.Exit(code=2)

    if response_file is not None:
        response = response_file.read_text(encoding="utf-8")
    else:
        response = sys.stdin.read()

    try:
        data = parse_and_validate(agent_type, response)
    except ContractError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(data, indent=2, sort_keys=True))


@app.command(name="review-packet", help=PLANNED_SUBCOMMANDS["review-packet"])
def review_packet(
    cr_ref: str = typer.Argument(
        ..., help="Change-request ref to bake: the PR number (GitHub) / MR iid (GitLab)."
    ),
    repo_root: Path = typer.Option(
        Path("."), "--repo-root",
        help="Repository whose origin remote decides the host adapter (gh/glab).",
    ),
    host: str | None = typer.Option(
        None, "--host", help="Override host auto-detection: github or gitlab."
    ),
    checks: str | None = typer.Option(
        None, "--checks",
        help="Test/coverage signals to embed verbatim in the packet's signals section.",
    ),
    max_chars: int | None = typer.Option(
        None, "--max-chars",
        help="Rendered-size cap; an oversized packet exits 3 (fallback) instead of truncating.",
        show_default="review_packet.PACKET_MAX_CHARS",
    ),
) -> None:
    """Bake the pre-baked review packet for one change request (Story 27.3-003).

    Prints the markdown packet — CR metadata, changed files, the full unified
    diff, and optional test/coverage signals — that the controller embeds into
    review prompts, so a reviewer (or FX) can produce the same artifact by
    hand. Exits 1 on a host failure and 3 when the rendered packet exceeds the
    cap: the consumer must then fall back to fetch-it-yourself review, never a
    truncated diff.
    """
    from sdlc.issue_host import IssueHostError, get_adapter, resolve_host
    from sdlc.review_packet import PACKET_MAX_CHARS, build_review_packet

    cap = max_chars if max_chars is not None else PACKET_MAX_CHARS
    try:
        adapter = get_adapter(resolve_host(repo_root, host))
        packet = build_review_packet(adapter, cr_ref, checks=checks)
    except IssueHostError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    rendered = packet.render()
    if len(rendered) > cap:
        typer.echo(
            f"error: rendered packet is {len(rendered)} chars (cap {cap}) — "
            "fall back to fetch-it-yourself review (gh pr view/diff or glab mr view/diff)",
            err=True,
        )
        raise typer.Exit(code=3)
    typer.echo(rendered)


@app.command(help=PLANNED_SUBCOMMANDS["rollback"])
def rollback(
    run: str | None = typer.Argument(
        None, help="Run id to roll back (default: the most recent run)."
    ),
    to: str = typer.Option(
        ...,
        "--to",
        help="Checkpoint story id to roll back to (kept; later stories reset).",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Roll a run back to a prior ledger checkpoint.

    ``--to <story_id>`` names the checkpoint to return to: that story and every
    story scheduled before it are kept untouched, while every story scheduled
    *after* it is reset to a fresh unbuilt state (stage history deleted, PR and
    branch cleared, status TODO). The run is reopened so the next ``sdlc
    resume``/``sdlc build`` rebuilds only the reset stories.

    Refuses (non-zero exit) when the checkpoint does not exist or when the
    rollback would discard a story whose PR has already merged — a merged PR is
    committed work the ledger cannot unwind. Rolling back to the latest story is
    a benign no-op.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.rollback import RollbackError, run_rollback

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before rollback reads/writes.
    ledger.ensure_migrated()
    try:
        result = run_rollback(ledger, run, to)
    except RollbackError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not result.reset_stories:
        typer.echo(
            f"nothing to roll back: '{to}' is already the latest checkpoint in "
            f"run {result.run_id[:8]}."
        )
        raise typer.Exit(code=0)

    typer.echo(
        f"rolled run {result.run_id[:8]} back to '{result.checkpoint}': "
        f"reset {len(result.reset_stories)} story(ies) "
        f"({', '.join(result.reset_stories)}) — "
        f"run `sdlc resume` to rebuild them."
    )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["reconcile"])
def reconcile(
    run: str | None = typer.Argument(
        None, help="Run id to reconcile (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Re-check a finished/interrupted run against ``origin/main``, then re-terminal.

    The manual counterpart to the automatic close-out reconciliation (Story
    12.3-001): a recovery verb in the same spirit as ``resume``/``rollback`` — not
    new orchestration. When an overnight run aborts (e.g. a 429) before its
    already-open PRs were merged by hand the next morning, the ledger can show
    FAILED days later even though the work shipped. ``sdlc reconcile`` runs the
    same ``reconcile_run`` algorithm as close-out: it fetches ``origin``,
    reclassifies any parked story whose ``feature/<id>`` work is provably on the
    base, re-stamps the run terminal, and prints a human summary.

    Defaults to the most recent run (mirrors ``rollback``). Idempotent — a run
    with nothing to reconcile reports "nothing to reconcile" and exits 0. Offline
    / no-remote degrades to a clean skip. No ledger / no runs reports cleanly;
    only a genuinely-unknown *explicit* run id exits non-zero, and no spurious
    empty ledger is ever created.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.reconcile import reconcile_run
    from sdlc.registry import Registry

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before reconcile reads it, so
    # an old DB never crashes with "no such column". No-op when no DB exists.
    ledger.ensure_migrated()

    # An explicit run id that does not exist is the one genuinely-unknown case
    # that exits non-zero; absence of any ledger/run reports cleanly below.
    if run is not None and ledger.run_row(run) is None:
        typer.echo(f"error: no such run '{run}' in ledger: {db_path}", err=True)
        raise typer.Exit(code=2)

    result = reconcile_run(ledger, run, registry=Registry())

    if not result.run_id:
        typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    if result.skipped:
        typer.echo(
            f"reconcile skipped for run {result.run_id[:8]}: could not fetch "
            "origin (offline / no remote)."
        )
        raise typer.Exit(code=0)

    if not result.changed:
        typer.echo(f"nothing to reconcile: run {result.run_id[:8]} unchanged.")
        raise typer.Exit(code=0)

    for item in result.reclassified:
        sha = item.get("sha") or ""
        typer.echo(
            f"  {item['story_id']}: {item['from_status']} → DONE "
            f"via {item['signal']}" + (f" ({sha[:8]})" if sha else "")
        )
    typer.echo(
        f"reconciled {len(result.reclassified)} story(ies) to DONE; "
        f"run {result.run_id[:8]} {result.run_status_before} → "
        f"{result.run_status_after}."
    )
    raise typer.Exit(code=0)


# How many residual (non-updated) rows `usage-reconcile` names individually. A
# repo whose transcripts were pruned by `sdlc clean` can carry hundreds of
# unverifiable rows; dumping them all would bury the summary. `--json` always
# carries the complete list.
_USAGE_RESIDUAL_LIST_CAP = 20


@app.command(name="usage-reconcile", help=PLANNED_SUBCOMMANDS["usage-reconcile"])
def usage_reconcile(
    run: str | None = typer.Argument(
        None, help="Run id to reconcile (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    all_runs: bool = typer.Option(
        False, "--all", help="Sweep every run in the ledger, not just the latest."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change without writing to the ledger."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the reconciliation report as a JSON object."
    ),
) -> None:
    """Backfill per-stage token/cost usage from the session logs (Story 28.1-001).

    The ledger is the cost record the estimator trains on, but it under-reported:
    a stage whose usage was never written (or was overwritten by a cheap recovery
    attempt) leaves the meter wrong forever. This pass re-derives each stage
    attempt's usage from its own transcript under ``.sdlc-state.db.logs/<run>/``
    and writes the log-derived figures onto the correct attempt row.

    The transcript's terminal ``{"type":"result"}`` line is the only authoritative
    cost record — per-turn stream-json events carry tokens but no dollars. A
    crashed or interrupted session therefore recovers **tokens only**, flagged
    ``log-recovered`` with cost left unavailable rather than fabricated.

    Idempotent: every write assigns the log's absolute totals (never accumulates),
    so re-running changes nothing. Stages still ``IN_PROGRESS`` are skipped, and a
    row whose transcript was pruned is reported *unverifiable* and left untouched
    — never counted as agreement. ``sdlc doctor`` reports the resulting agreement
    rate on every health check.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.usage_reconcile import reconcile_usage

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing ledger before the pass reads/writes `usage_source`.
    # No-op when no DB exists, so a never-built repo keeps a clean absence.
    ledger.ensure_migrated()

    if run is not None and ledger.run_row(run) is None:
        typer.echo(f"error: no such run '{run}' in ledger: {db_path}", err=True)
        raise typer.Exit(code=2)

    result = reconcile_usage(ledger, run, all_runs=all_runs, apply=not dry_run)

    if as_json:
        typer.echo(json.dumps(result.to_dict(), default=str))
        raise typer.Exit(code=0)

    if not result.run_ids:
        typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    for audit in result.updated:
        cost = "cost unavailable (crashed session)"
        if audit.log_cost_usd is not None:
            cost = f"${audit.log_cost_usd:.2f}"
        typer.echo(
            f"  {audit.label}: backfilled {audit.log_tokens:,} tokens, {cost} "
            f"[{audit.reason}]"
        )
    pending = [a for a in result.residual if not a.updated]
    for audit in pending[:_USAGE_RESIDUAL_LIST_CAP]:
        typer.echo(f"  {audit.label}: {audit.reason}")
    if len(pending) > _USAGE_RESIDUAL_LIST_CAP:
        typer.echo(
            f"  … +{len(pending) - _USAGE_RESIDUAL_LIST_CAP} more residual row(s) "
            "(use --json for the full list)"
        )

    if dry_run:
        # Nothing was written, so "updated" is always empty — report the residual
        # disagreements the pass *would* act on instead of a misleading zero.
        summary = (
            f"usage-reconcile (dry-run): {len(result.residual)} "
            "residual disagreement(s)"
        )
    else:
        summary = f"usage-reconcile: {len(result.updated)} row(s) updated"
    if result.agreement_rate is not None:
        summary += (
            f"; agreement {result.matched}/{result.verifiable} "
            f"({result.agreement_rate:.0%})"
        )
    if result.unverifiable:
        summary += f"; {result.unverifiable} unverifiable (no readable session log)"
    if result.skipped_in_progress:
        summary += f"; {result.skipped_in_progress} in-progress row(s) skipped"
    typer.echo(summary + ".")
    raise typer.Exit(code=0)


@app.command(name="model-backfill", help=PLANNED_SUBCOMMANDS["model-backfill"])
def model_backfill(
    run: str | None = typer.Argument(
        None, help="Run id to backfill (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    all_runs: bool = typer.Option(
        False, "--all", help="Sweep every run in the ledger, not just the latest."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change without writing to the ledger."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the backfill report as a JSON object."
    ),
) -> None:
    """Backfill the ledger's per-stage `model` from the session logs (Story 28.1-002).

    ``stages.model`` is what makes cost-by-model a fact in the ledger rather than
    something re-derived by parsing logs — but every attempt recorded before the
    verified per-attempt recording landed reads NULL. This pass re-reads each
    attempt's own transcript under ``.sdlc-state.db.logs/<run>/`` and writes the
    model the session actually ran on onto that exact row.

    The transcript's terminal ``{"type":"result"}`` envelope carries a
    ``modelUsage`` map — the authoritative record of every model the session
    touched; the one that carried the session (by cost, then output tokens) is
    what lands. A session that crashed before that envelope falls back to the
    model its assistant turns name.

    **Never coerced.** A row whose transcript was pruned, or which only ever held
    plain-text output, stays NULL and is *counted* as unrecoverable, so the
    column's true coverage is known instead of papered over with a placeholder. A
    row that already attributes a model is never overwritten.

    Idempotent: a second run finds every backfilled row already recorded and
    writes nothing. ``sdlc doctor`` reports the resulting coverage on every
    health check and fails on a fresh-run regression to NULL.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.model_backfill import backfill_models

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing ledger before the pass reads/writes `model`. No-op
    # when no DB exists, so a never-built repo keeps a clean absence.
    ledger.ensure_migrated()

    if run is not None and ledger.run_row(run) is None:
        typer.echo(f"error: no such run '{run}' in ledger: {db_path}", err=True)
        raise typer.Exit(code=2)

    result = backfill_models(ledger, run, all_runs=all_runs, apply=not dry_run)

    if as_json:
        typer.echo(json.dumps(result.to_dict(), default=str))
        raise typer.Exit(code=0)

    if not result.run_ids:
        typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    for audit in result.updated:
        typer.echo(f"  {audit.label}: model={audit.log_model} [{audit.reason}]")
    pending = [a for a in result.residual if not a.updated]
    for audit in pending[:_USAGE_RESIDUAL_LIST_CAP]:
        typer.echo(f"  {audit.label}: {audit.reason}")
    if len(pending) > _USAGE_RESIDUAL_LIST_CAP:
        typer.echo(
            f"  … +{len(pending) - _USAGE_RESIDUAL_LIST_CAP} more row(s) without a "
            "model (use --json for the full list)"
        )

    if dry_run:
        # Nothing was written, so "updated" is always empty — report the rows the
        # pass *would* attribute instead of a misleading zero.
        summary = (
            f"model-backfill (dry-run): {len(result.residual)} row(s) without a model"
        )
    else:
        summary = f"model-backfill: {len(result.updated)} row(s) updated"
    if result.coverage is not None:
        summary += (
            f"; coverage {result.populated}/{result.dispatched} "
            f"({result.coverage:.0%})"
        )
    if result.unrecoverable:
        summary += f"; {result.unrecoverable} unrecoverable (no model in the session log)"
    typer.echo(summary + ".")
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["clean"])
def clean(
    force: bool = typer.Option(
        False,
        "--force",
        "--yes",
        help="Actually remove the candidates. Without it, clean is dry-run only.",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the plan as JSON instead of a human summary."
    ),
) -> None:
    """Safely garbage-collect build leftovers — dry-run by default.

    Reclaims three classes of cruft a finished/crashed build leaves behind:
    orphaned ``agent-*`` git worktrees, squash-merged ``feature/<id>`` branches,
    and stale ``.sdlc-state.db.logs/`` transcript dirs. It is **dry-run by
    default** — it reports what it *would* remove and removes nothing until
    ``--force``/``--yes`` is passed.

    Safe to run beside a live build: every candidate is cross-checked against the
    host run registry + a live-pid probe, so a worktree or branch an
    ``IN_PROGRESS`` run owns (here or in another session/clone) is never touched.
    "Merged" is decided by the ledger (``status=DONE``) and the PR's merge state —
    not ``git branch --merged``, which misreports squash-merges. Removals are
    recoverable where git allows (a deleted branch's tip stays reachable via
    reflog) and the command never pushes to or fetches from the remote. Exits 0.
    """
    from sdlc.clean import run_clean
    from sdlc.ledger_view import default_db_path

    db_path = db or default_db_path()
    plan = run_clean(db_path=db_path, force=force)

    if as_json:
        typer.echo(json.dumps(plan.to_dict(), default=str))
        raise typer.Exit(code=0)

    verb = "removed" if force else "would remove"
    if not plan.candidates:
        typer.echo("clean: nothing to do — workspace already tidy.")
        raise typer.Exit(code=0)

    for kind, label in (("worktree", "worktrees"), ("branch", "branches"), ("logs", "logs")):
        items = plan.by_kind(kind)
        if not items:
            continue
        typer.echo(f"{label} ({len(items)}):")
        for item in items:
            mark = "✓" if item.removed else ("·" if not force else "✗")
            target = item.path or item.name
            typer.echo(f"  {mark} {verb}: {target} — {item.reason}")

    if force:
        typer.echo(f"clean: removed {plan.removed_count}/{plan.total} candidate(s).")
        for err in plan.errors:
            typer.echo(f"  warning: {err}", err=True)
    else:
        typer.echo(
            f"clean: {plan.total} candidate(s) — dry-run, nothing removed. "
            "Re-run with --force to act."
        )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["sast"])
def sast(
    report_file: Path | None = typer.Argument(
        None,
        help="semgrep --json report. Reads stdin when omitted.",
    ),
    config_file: Path = typer.Option(
        Path(".sast-config.yaml"),
        "--config",
        help="Per-repo SAST overrides (suppressions, extra rulesets).",
    ),
) -> None:
    """Classify a semgrep report into a SAST gate verdict.

    Reads a semgrep ``--json`` report (from a file or stdin), applies any
    per-repo suppressions from ``.sast-config.yaml``, and prints the verdict as
    ``SAST_STATUS: CLEAN | WARN | BLOCK`` followed by one line per gating
    finding. Exits 0 for CLEAN/WARN and 1 for BLOCK so a shell gate can branch
    on the exit code.
    """
    from sdlc.security_scan import (
        SastConfigError,
        SastReportError,
        classify_report,
        load_sast_config,
    )

    report = (
        report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()
    )

    try:
        config = load_sast_config(config_file)
        result = classify_report(report, config=config)
    except (SastReportError, SastConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"SAST_STATUS: {result.status}")
    for finding in result.findings:
        typer.echo(f"  [{finding.severity}] {finding.location()} {finding.check_id}")
    for finding in result.suppressed:
        typer.echo(f"  [suppressed] {finding.location()} {finding.check_id}")

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(help=PLANNED_SUBCOMMANDS["depscan"])
def depscan(
    report_file: Path | None = typer.Argument(
        None,
        help="osv-scanner --format=json report. Reads stdin when omitted.",
    ),
    suppressions_file: Path = typer.Option(
        Path(".dep-scan-suppressions.yaml"),
        "--suppressions",
        help="Per-repo OSV-ID suppressions (each needs a reason and an expiry).",
    ),
) -> None:
    """Classify an osv-scanner report into a dependency-scan gate verdict.

    Reads an osv-scanner ``--format=json`` report (from a file or stdin),
    applies any per-repo suppressions from ``.dep-scan-suppressions.yaml``, and
    prints the verdict as ``DEP_SCAN_STATUS: CLEAN | WARN | BLOCK`` followed by
    one line per gating finding. Exits 0 for CLEAN/WARN and 1 for BLOCK so a
    shell gate can branch on the exit code. A malformed report or an expired
    suppression exits 2.
    """
    from sdlc.dependency_scan import (
        DepScanConfigError,
        DepScanReportError,
        classify_osv_report,
        load_dep_scan_suppressions,
    )

    report = (
        report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()
    )

    try:
        suppressions = load_dep_scan_suppressions(suppressions_file)
        result = classify_osv_report(report, suppressions=suppressions)
    except (DepScanReportError, DepScanConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"DEP_SCAN_STATUS: {result.status}")
    for finding in result.findings:
        typer.echo(
            f"  [{finding.severity}] {finding.coordinate()} "
            f"({finding.ecosystem}) {finding.osv_id}"
        )
    for finding in result.suppressed:
        typer.echo(f"  [suppressed] {finding.coordinate()} {finding.osv_id}")

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(help=PLANNED_SUBCOMMANDS["supplychain"])
def supplychain(
    root: Path = typer.Argument(
        Path("."),
        help="Repo root to scan (hooks/, skills/, plugins/*/skills/, mcp, settings.json).",
    ),
    allowlist_file: Path = typer.Option(
        Path(".supply-chain-allowlist.yaml"),
        "--allowlist",
        help="Per-finding allowlist (each entry needs path, pattern, and reason).",
    ),
) -> None:
    """Scan installed hooks/skills/MCP/settings for dangerous patterns.

    Treats those config artifacts as supply-chain inputs and flags egress tools,
    MCP auto-trust, ANTHROPIC_BASE_URL overrides, encoded payloads, and hidden
    Unicode. Prints ``SUPPLY_CHAIN_STATUS: CLEAN | WARN | BLOCK`` followed by one
    line per gating finding (file, line, pattern). Exits 0 for CLEAN/WARN and 1
    for BLOCK so a CI gate can branch on the exit code. A malformed allowlist
    exits 2.
    """
    from sdlc.supply_chain_scan import (
        SupplyChainConfigError,
        load_allowlist,
        scan_repo,
    )

    try:
        allowlist = load_allowlist(allowlist_file)
        result = scan_repo(root, allowlist=allowlist)
    except SupplyChainConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"SUPPLY_CHAIN_STATUS: {result.status}")
    for finding in result.findings:
        # The sha256 is printed so an operator can copy path/line/pattern/sha256
        # straight into a .supply-chain-allowlist.yaml entry.
        typer.echo(
            f"  [{finding.band}] {finding.location()} {finding.pattern_id}"
            f" sha256:{finding.digest} — {finding.description}"
        )
    for finding in result.suppressed:
        typer.echo(
            f"  [suppressed] {finding.location()} {finding.pattern_id}"
            f" sha256:{finding.digest}"
        )

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(name="sync-check", help=PLANNED_SUBCOMMANDS["sync-check"])
def sync_check(
    source_dir: Path = typer.Argument(
        ...,
        help="Source-of-truth shared-skills directory (this repo). With "
        "--neutral, the directory holding the committed generated bodies.",
    ),
    consumer_dir: Path = typer.Argument(
        None,
        help="Consumer submodule checkout (e.g. the nix-install Codex mirror). "
        "Optional when only a --neutral parity check is wanted.",
        show_default=False,
    ),
    neutral_dir: Path = typer.Option(
        None,
        "--neutral",
        help="Harness-neutral skill sources dir. When set, also verify that "
        "SOURCE_DIR's committed bodies match what these sources generate "
        "(Story 20.4-003).",
        show_default=False,
    ),
    skill_base: Path = typer.Option(
        None,
        "--skill-base",
        help="Plugin skills base dir (e.g. plugins/autonomous-sdlc/skills). When "
        "set with --neutral, also verify that every committed pipeline "
        "<name>/SKILL.md matches what its neutral source generates (Story "
        "20.7-002).",
        show_default=False,
    ),
    harness: str = typer.Option(
        "claude",
        "--harness",
        help="Harness whose generated body to compare against the neutral "
        "source (default: claude).",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Regenerate SOURCE_DIR's bodies (and --skill-base SKILL.md files) "
        "from the neutral sources instead of reporting drift (requires "
        "--neutral).",
    ),
) -> None:
    """Verify shared-skill parity — between repos, and against neutral sources.

    Two complementary checks, either or both run in one invocation:

    * Consumer mirror (``sync-check SOURCE CONSUMER``): a consumer's
      shared-skills submodule mirrors the source of truth byte-for-byte. Run
      after ``git submodule update --remote``.
    * Generated parity (``sync-check SOURCE --neutral NEUTRAL``): every committed
      generated body under SOURCE matches what its harness-neutral source
      regenerates, so Claude and Codex skill files cannot silently diverge.
      ``--fix`` rewrites the bodies from the sources instead of reporting drift.
    * Pipeline parity (``--skill-base BASE`` with ``--neutral``): every committed
      pipeline ``<name>/SKILL.md`` under BASE matches what its neutral source
      generates (Story 20.7-002). Folds into the same exit code and ``--fix``.

    Exits 0 when everything is in sync, 1 on drift, and 2 when a directory is
    missing or a neutral source is malformed.
    """
    from sdlc.skill_format import SkillFormatError
    from sdlc.sync import (
        GeneratedState,
        SkillState,
        generated_parity_report,
        parity_report,
        pipeline_parity_report,
        write_generated_skills,
        write_pipeline_skills,
    )

    if skill_base is not None and neutral_dir is None:
        typer.echo("error: --skill-base requires --neutral.", err=True)
        raise typer.Exit(code=2)

    if neutral_dir is None and consumer_dir is None:
        typer.echo(
            "error: provide a CONSUMER_DIR and/or --neutral NEUTRAL_DIR.", err=True
        )
        raise typer.Exit(code=2)

    if fix and neutral_dir is None:
        typer.echo("error: --fix requires --neutral.", err=True)
        raise typer.Exit(code=2)

    exit_code = 0

    if neutral_dir is not None:
        try:
            if fix:
                written = write_generated_skills(
                    neutral_dir, source_dir, harness=harness
                )
                typer.echo(
                    f"regenerated {len(written)} {harness} skill(s) "
                    f"from {neutral_dir}."
                )
            else:
                report = generated_parity_report(
                    neutral_dir, source_dir, harness=harness
                )
                if report.in_sync:
                    typer.echo(
                        f"generated {harness} skills in sync "
                        f"({len(report.skills)} skills)."
                    )
                else:
                    for skill in report.skills:
                        if skill.state is not GeneratedState.IN_SYNC:
                            typer.echo(f"{skill.name}: {skill.state.value}")
                            if skill.diff:
                                typer.echo(skill.diff)
                    typer.echo(
                        "generated skills drifted — regenerate with "
                        f"`sdlc sync-check {source_dir} --neutral {neutral_dir} "
                        f"--harness {harness} --fix`."
                    )
                    exit_code = 1
        except (FileNotFoundError, SkillFormatError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    if skill_base is not None:
        try:
            if fix:
                written = write_pipeline_skills(
                    neutral_dir, skill_base, harness=harness
                )
                typer.echo(
                    f"regenerated {len(written)} {harness} pipeline skill(s) "
                    f"into {skill_base}."
                )
            else:
                report = pipeline_parity_report(
                    neutral_dir, skill_base, harness=harness
                )
                if report.in_sync:
                    typer.echo(
                        f"generated {harness} pipeline skills in sync "
                        f"({len(report.skills)} skills)."
                    )
                else:
                    for skill in report.skills:
                        if skill.state is not GeneratedState.IN_SYNC:
                            typer.echo(f"{skill.name}: {skill.state.value}")
                            if skill.diff:
                                typer.echo(skill.diff)
                    typer.echo(
                        "pipeline skills drifted — regenerate with "
                        f"`sdlc sync-check {source_dir} --neutral {neutral_dir} "
                        f"--skill-base {skill_base} --harness {harness} --fix` "
                        "(or `scripts/generate-skills.sh generate`)."
                    )
                    exit_code = 1
        except (FileNotFoundError, SkillFormatError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    if consumer_dir is not None:
        try:
            report = parity_report(source_dir, consumer_dir)
        except FileNotFoundError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

        if report.in_sync:
            typer.echo(f"shared skills in sync ({len(report.skills)} skills).")
        else:
            for skill in report.skills:
                if skill.state is not SkillState.IN_SYNC:
                    typer.echo(f"{skill.name}: {skill.state.value}")
            typer.echo("shared skills drifted — run `git submodule update --remote`.")
            exit_code = 1

    raise typer.Exit(code=exit_code)


@app.command(name="generate-skills", help=PLANNED_SUBCOMMANDS["generate-skills"])
def generate_skills(
    neutral_dir: Path = typer.Argument(
        ...,
        help="Neutral skill sources directory (e.g. shared-skills/neutral).",
    ),
    claude_base: Path = typer.Option(
        ...,
        "--claude-base",
        help="Base dir for Claude skills (e.g. plugins/autonomous-sdlc/skills).",
    ),
    codex_base: Path = typer.Option(
        ...,
        "--codex-base",
        help="Base dir for Codex skills (the nix-install mirror's skills dir).",
    ),
) -> None:
    """Generate Claude + Codex ``SKILL.md`` files from the neutral sources.

    One authored neutral source per skill emits both harness files, so the two
    runtimes stay in lock-step automatically. Exits 0 on success and 2 when the
    neutral directory is missing or a source fails to parse.
    """
    from sdlc.skill_generator import SkillGeneratorError, generate_all

    try:
        generated = generate_all(neutral_dir, claude_base, codex_base)
    except (FileNotFoundError, SkillGeneratorError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    for skill in generated:
        targets = [
            label
            for label, path in (
                ("claude", skill.claude_path),
                ("codex", skill.codex_path),
            )
            if path is not None
        ]
        typer.echo(f"{skill.name}: {', '.join(targets)}")
    typer.echo(f"generated {len(generated)} skill(s).")


@app.command(help=PLANNED_SUBCOMMANDS["repair"])
def repair(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be restored without changing anything.",
    ),
    claude_dir: Path = typer.Option(
        None,
        "--claude-dir",
        help="Claude config dir to repair (default: ~/.claude).",
        show_default=False,
    ),
    root: Path = typer.Option(
        None,
        "--root",
        help="Framework repo root that owns the managed set (default: this install).",
        show_default=False,
    ),
) -> None:
    """Restore the framework's managed symlinks/config without a full reinstall.

    Recreates any of the install's managed symlinks (``CLAUDE.md``, ``agents/``,
    ``hooks/``, the plugin marketplace, …) that are missing or have drifted to a
    wrong target. A real file occupying a managed slot is moved into a timestamped
    backup dir (never deleted) before linking. Idempotent: a healthy install is a
    no-op, and nothing outside the managed set is ever touched. ``--dry-run``
    reports the plan without acting.
    """
    from sdlc.repair import (
        RepairAction,
        WorktreeRootError,
        apply_plan,
        build_plan,
        default_backup_dir,
        default_claude_dir,
        default_repo_root,
    )

    repo_root = (root or default_repo_root()).resolve()
    cdir = claude_dir or default_claude_dir()

    try:
        plan = build_plan(repo_root, cdir)
    except WorktreeRootError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if plan.healthy:
        typer.echo(
            f"install healthy — {len(plan.artifacts)} managed artifacts in place, "
            "nothing to repair."
        )
        raise typer.Exit(code=0)

    results = apply_plan(plan, dry_run=dry_run, backup_dir=default_backup_dir(cdir))

    prefix = "[dry-run] would " if dry_run else ""
    restored = 0
    for r in results:
        a = r.artifact
        if r.action is RepairAction.LINKED:
            typer.echo(f"{prefix}link {a.rel_dest} → {a.src} (was missing)")
        elif r.action is RepairAction.RELINKED:
            typer.echo(f"{prefix}relink {a.rel_dest} → {a.src} (pointed elsewhere)")
        elif r.action is RepairAction.BACKED_UP:
            typer.echo(
                f"{prefix}back up {a.rel_dest} to {r.backup_path} and link → {a.src} "
                "(a real file occupied the slot)"
            )
        else:
            continue
        restored += 1

    verb = "would restore" if dry_run else "restored"
    typer.echo(f"{verb} {restored} managed artifact(s).")
    raise typer.Exit(code=0)


@app.command(name="eval", help=PLANNED_SUBCOMMANDS["eval"])
def eval_cmd(
    config_file: Path = typer.Option(
        Path("eval/eval-config.yaml"),
        "--config",
        help="Versioned eval config (sample target + fixed ticket set + n runs).",
    ),
    n: int = typer.Option(
        None,
        "--n",
        help="Override the config's runs-per-ticket (e.g. --n 1 for a quick pass).",
        show_default=False,
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the scoreboard as JSON instead of a text table.",
    ),
    workspace: Path = typer.Option(
        None,
        "--workspace",
        help="Throwaway dir for per-run git checkouts (default: a temp dir).",
        show_default=False,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the tickets the eval would run and exit (dispatch nothing).",
    ),
) -> None:
    """Run the agentic eval harness over a fixed ticket set and emit a scoreboard.

    Drives the build agent headlessly against a versioned sample target — once per
    ticket × ``n`` runs, each in an isolated git checkout — and scores every result
    on LOC delta, token usage, notional cost, wall-time, and a quality check (the
    ticket's ``quality_cmd``, exit 0 = pass). The framework repo and the sample
    target are never mutated and no PRs are opened. ``--dry-run`` lists the tickets
    without spending any quota. A malformed config exits 2.
    """
    import tempfile

    from sdlc.evaluate import (
        EvalConfig,
        EvalConfigError,
        aggregate,
        load_config,
        render_table,
        run_eval,
        scoreboard_to_dict,
    )

    try:
        config = load_config(config_file)
    except EvalConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if n is not None:
        if n < 1:
            typer.echo("error: --n must be >= 1", err=True)
            raise typer.Exit(code=2)
        config = EvalConfig(
            name=config.name,
            target=config.target,
            tickets=config.tickets,
            n=n,
            seed=config.seed,
            agent_type=config.agent_type,
            usd_per_million_tokens=config.usd_per_million_tokens,
            model=config.model,
        )

    if dry_run:
        typer.echo(
            f"eval: {config.name} — {len(config.tickets)} ticket(s) × {config.n} run(s) "
            f"against {config.target}"
        )
        for ticket in config.tickets:
            typer.echo(f"  - {ticket.id}")
        raise typer.Exit(code=0)

    with tempfile.TemporaryDirectory(prefix="sdlc-eval-") as tmp:
        ws = workspace or Path(tmp)
        ws.mkdir(parents=True, exist_ok=True)
        results = run_eval(config, ws)

    board = aggregate(results, config.name)
    if as_json:
        typer.echo(json.dumps(scoreboard_to_dict(board), indent=2))
    else:
        typer.echo(render_table(board))
    raise typer.Exit(code=0)


@app.command(name="eval-compare", help=PLANNED_SUBCOMMANDS["eval-compare"])
def eval_compare_cmd(
    baseline: Path = typer.Option(
        ...,
        "--baseline",
        help="Variant-A scoreboard JSON (from `sdlc eval --json`).",
    ),
    candidate: Path = typer.Option(
        ...,
        "--candidate",
        help="Variant-B scoreboard JSON to compare against the baseline.",
    ),
    tolerance: float = typer.Option(
        DEFAULT_TOLERANCE,
        "--tolerance",
        help="Relative move (fraction of baseline) before a metric flags as better/worse.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the comparison as JSON instead of a text table.",
    ),
    out: Path = typer.Option(
        None,
        "--out",
        help="Also persist the comparison JSON to this path (records the A/B decision).",
        show_default=False,
    ),
) -> None:
    """Compare two eval scoreboards on the same tickets — per-metric delta + verdict.

    Reads two scoreboard JSON files (variant A vs B: a prompt/skill/model change run
    through `sdlc eval --json`), produces a side-by-side delta and a clear
    better/worse/neutral verdict per ticket and overall, and can persist the
    comparison (`--out`) so a prompt/model decision is backed by data, not vibes. A
    malformed scoreboard exits 2.
    """
    from sdlc.eval_compare import (
        BaselineError,
        comparison_to_dict,
        compare_scoreboards,
        load_scoreboard,
        render_comparison_table,
    )

    try:
        base_board = load_scoreboard(baseline)
        cand_board = load_scoreboard(candidate)
    except BaselineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    comparison = compare_scoreboards(base_board, cand_board, tolerance=tolerance)
    payload = comparison_to_dict(comparison)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(render_comparison_table(comparison))
    raise typer.Exit(code=0)


@app.command(name="eval-baseline", help=PLANNED_SUBCOMMANDS["eval-baseline"])
def eval_baseline_cmd(
    baseline: Path = typer.Option(
        Path("eval/baseline.json"),
        "--baseline",
        help="Committed baseline scoreboard to check against (or write with --update).",
    ),
    candidate: Path = typer.Option(
        None,
        "--candidate",
        help="Fresh scoreboard JSON (from `sdlc eval --json`) to check / promote.",
        show_default=False,
    ),
    tolerance: float = typer.Option(
        DEFAULT_TOLERANCE,
        "--tolerance",
        help="Relative move (fraction of baseline) before a metric counts as a regression.",
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help="Promote the candidate to the baseline file (no regression check).",
    ),
    warn_only: bool = typer.Option(
        False,
        "--warn-only",
        help="Report regressions but still exit 0 (advisory, not a gate).",
    ),
) -> None:
    """Flag metrics that regressed beyond tolerance vs a committed baseline.

    Compares a fresh scoreboard (`--candidate`, from `sdlc eval --json`) against the
    committed baseline and lists any metric that got materially worse (quality down,
    or LOC/tokens/cost/wall up beyond `--tolerance`). Exits 1 when regressions are
    found (unless `--warn-only`), 0 when clean. `--update` promotes the candidate to
    the baseline file instead of checking. A malformed file exits 2.
    """
    from sdlc.eval_compare import (
        BaselineError,
        compare_scoreboards,
        load_scoreboard,
        regressions,
        save_scoreboard,
    )

    if candidate is None:
        typer.echo("error: --candidate is required", err=True)
        raise typer.Exit(code=2)

    try:
        cand_board = load_scoreboard(candidate)
    except BaselineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if update:
        save_scoreboard(cand_board, baseline)
        typer.echo(f"baseline updated: {baseline}")
        raise typer.Exit(code=0)

    try:
        base_board = load_scoreboard(baseline)
    except BaselineError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    comparison = compare_scoreboards(base_board, cand_board, tolerance=tolerance)
    flagged = regressions(comparison)
    if not flagged:
        typer.echo(
            f"baseline OK: no regressions beyond {tolerance:.0%} "
            f"({comparison.candidate_name} vs {comparison.baseline_name})"
        )
        raise typer.Exit(code=0)

    typer.echo(f"regressions vs baseline (tolerance {tolerance:.0%}):", err=True)
    for ticket_id, metric in flagged:
        base = "—" if metric.baseline is None else f"{metric.baseline:.4g}"
        cand = "—" if metric.candidate is None else f"{metric.candidate:.4g}"
        pct = "—" if metric.pct is None else f"{metric.pct * 100:+.0f}%"
        typer.echo(f"  {ticket_id}: {metric.label} {base} -> {cand} ({pct})", err=True)
    raise typer.Exit(code=0 if warn_only else 1)


# --- `sdlc issues` command group (Epic-22) ----------------------------------
# A dedicated group for the host story-mirror verbs. The bare `init` verb is
# deliberately *not* reused (Epic-10 removed it); host backfill lives under
# `issues` so its scope is unmistakable.
issues_app = typer.Typer(
    name="issues",
    help="Mirror the story backlog onto a code host (GitHub/GitLab).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(issues_app, name="issues")


@issues_app.command(name="init")
def issues_init(
    host: str | None = typer.Option(
        None, "--host", help="github|gitlab (default: auto-detect from origin)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    root: Path | None = typer.Option(
        None, "--root", help="Repo root holding docs/stories (default: cwd)."
    ),
) -> None:
    """Backfill the full board: an issue for every story across every epic.

    The one command to adopt a repo (Story 22.3-001). Provisions the taxonomy and
    creates one issue per story via the host adapter, recording each mapping in the
    inventory. A story already Done is created **and immediately closed**, so the
    board shows full history while the open-issues list stays = real remaining
    work. Idempotent: an interrupted or rate-limited run resumes cheaply —
    already-mapped stories are updated, never duplicated.

    With no framework-format stories it exits 1 pointing at ``generate-epics``; an
    undeterminable/unsupported host or an unauthenticated CLI exits 2.
    """
    from sdlc.issue_host import IssueHostError, get_adapter, resolve_host
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.story_init import NoStoriesError, init_issues
    from sdlc.story_render import parse_story_docs

    root_path = root or Path.cwd()

    # No-stories guidance is independent of host auth — check before any host work.
    if not parse_story_docs(root_path):
        typer.echo(
            "no framework-format stories found under docs/stories/; "
            "run `generate-epics` to author them first",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        resolved = resolve_host(root_path, host)
        adapter = get_adapter(resolved)
        adapter.ensure_ready()
    except IssueHostError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    db_path = db or default_db_path(root_path)
    ledger = Ledger(db_path)
    # init (not ensure_migrated) so adopting a never-built repo provisions the
    # schema; idempotent CREATE-IF-NOT-EXISTS leaves an existing ledger intact.
    ledger.init()

    try:
        result = init_issues(adapter, ledger, root=root_path)
    except NoStoriesError as exc:  # defensive — the pre-check above usually wins
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except IssueHostError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    actions: dict[str, int] = {}
    for outcome in result.outcomes:
        actions[outcome.action] = actions.get(outcome.action, 0) + 1
    breakdown = ", ".join(f"{n} {action}" for action, n in sorted(actions.items()))
    typer.echo(
        f"init {result.host}: {result.total} story(ies) backfilled"
        + (f" ({breakdown})" if breakdown else "")
        + f"; {len(result.closed)} Done issue(s) closed."
    )
    raise typer.Exit(code=0)


# `assign` (Story 22.5-002) is the one place a CLI writes ownership *to* the host
# — the host (GitHub/GitLab) stays authoritative; the ledger `owner` is the
# cached read.
@issues_app.command("assign")
def issues_assign(
    target: str = typer.Argument(
        ...,
        help="A story id (NN.F-NNN) to assign one story, or an epic id (epic-NN) "
        "to cascade to every story in that epic.",
    ),
    user: str = typer.Argument(..., help="Host login/username to assign the work to."),
    host: str | None = typer.Option(
        None,
        "--host",
        help="Force the code host (github|gitlab); default: auto-detect from the "
        "git remote.",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Assign a single story or a whole epic to a host user.

    ``sdlc issues assign <story-id> <user>`` sets that story issue's assignee on
    the host and caches the ``owner`` locally. ``sdlc issues assign epic-NN
    <user>`` cascades to every story in the epic in one idempotent pass. The host
    (GitHub/GitLab) stays authoritative — the ledger ``owner`` is the cached read.

    Fails fast (exit 2) on an unknown user, a malformed target, or an epic with no
    stories — nothing is half-assigned. Exits 1 when one or more requested stories
    have no issue on this host (reported, never silently skipped).
    """
    from sdlc.issue_host import IssueHostError, get_adapter, resolve_host
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.story_assign import AssignError, assign

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # init (not ensure_migrated) so assigning against a never-mirrored repo
    # provisions the schema rather than reading a missing `story_inventory` table
    # (ensure_migrated is a no-op when the DB is absent, which left the inventory
    # SELECT crashing with an uncaught OperationalError). Idempotent
    # CREATE-IF-NOT-EXISTS leaves an existing ledger intact, same as `issues init`.
    ledger.init()

    try:
        resolved = resolve_host(Path.cwd(), override=host)
        adapter = get_adapter(resolved)
        # Fail fast on an unauthenticated host before any assignment.
        adapter.ensure_ready()
        result = assign(adapter, ledger, target, user)
    except (IssueHostError, AssignError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    scope = f"epic {result.target}" if result.is_epic else f"story {result.target}"
    typer.echo(
        f"{scope} → {result.user} ({resolved}): "
        f"{len(result.assigned)} assigned, {len(result.already)} already, "
        f"{len(result.unmapped)} unmapped"
    )
    for sid in result.assigned:
        typer.echo(f"  ✓ {sid}")
    for sid in result.already:
        typer.echo(f"  · {sid} (already {result.user})")
    for sid in result.unmapped:
        typer.echo(
            f"  ! {sid} — no issue on {resolved}; mirror it first "
            "(`sdlc issues init`)",
            err=True,
        )
    # An unmapped story means the pass could not cover everything — exit non-zero
    # so a partial run is never read as a clean success (Story 22.5-002 AC3).
    raise typer.Exit(code=1 if result.unmapped else 0)


if __name__ == "__main__":
    app()
