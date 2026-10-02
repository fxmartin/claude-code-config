# ABOUTME: Shared pytest fixtures for the sdlc controller test suite.
# ABOUTME: Mutes real Telegram sends and host auto-detection so tests never hit the network.

from __future__ import annotations

import pytest

from sdlc import fix_issue, issue_host


@pytest.fixture(autouse=True)
def _mute_coverage_precheck(monkeypatch):
    """Make the coverage pre-check inconclusive for every test by default.

    The pre-check (Story 27.3-001) resolves the *current working directory's*
    test command on the sequential path — under pytest that is the controller
    repo itself, so an un-stubbed pre-check would recursively run this very
    suite inside any test that drives ``run_build`` through the coverage
    stage. ``None`` (inconclusive) reproduces the pre-27.3-001 dispatch
    behavior byte-for-byte; the gate's own tests override this explicitly.
    """
    import sdlc.coverage_precheck as precheck_mod

    monkeypatch.setattr(
        precheck_mod, "run_precheck", lambda root, base_ref, branch, timeout=600: None
    )


@pytest.fixture(autouse=True)
def _mute_lifecycle_notifications(monkeypatch):
    """Disable real Telegram sends for every test by default.

    Production call sites in build.py / resume.py invoke ``sdlc.notify.notify``,
    which falls back to ``~/.claude/config/.env`` for credentials. On a developer
    machine those creds exist, so an un-muted suite would POST real messages.
    Setting ``SDLC_NOTIFY=off`` makes ``notify`` a guaranteed no-op. The notifier's
    own tests (test_notify.py) re-enable it explicitly via their own fixture.
    """
    monkeypatch.setenv("SDLC_NOTIFY", "off")


@pytest.fixture(autouse=True)
def _no_fleet_queue(monkeypatch, tmp_path_factory):
    """Keep every test on the local SQLite queue unless it opts into a fleet URL.

    `SDLC_QUEUE_URL`, a `.sdlc-queue.yaml` or a developer's real
    `~/.sdlc-fleet.yaml` would otherwise route queue consumers to a live
    service (Story 35.1-002). Tests that exercise the switch set the variable.
    """
    import sdlc.queue_client as queue_client

    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.delenv("SDLC_QUEUE_TOKEN", raising=False)
    nohome = tmp_path_factory.mktemp("no-fleet-home")
    monkeypatch.setattr(queue_client, "_home", lambda: nohome)


@pytest.fixture(autouse=True)
def _no_real_worker_launch_agent(monkeypatch, tmp_path_factory):
    """Keep a developer Mac's installed worker LaunchAgent out of `sdlc doctor` (Story 35.2-004).

    Where `~/Library/LaunchAgents/com.fxmartin.sdlc-worker.plist` exists, doctor
    reads that machine's live queue for its `fleet-worker` finding. Tests that
    exercise the check pass `agent_path` / `worker_plist` explicitly.
    """
    from sdlc import doctor

    absent = tmp_path_factory.getbasetemp() / "no-launch-agents" / f"{doctor.WORKER_LABEL}.plist"
    monkeypatch.setattr(doctor, "default_worker_plist", lambda: absent)


@pytest.fixture(autouse=True)
def _no_real_model_probe(monkeypatch):
    """Never spend a real ``claude -p`` call on the model entitlement probe.

    Reports "command not found", which the probe reads as inconclusive: no id is
    substituted and no ``--fallback-model`` is added, so argv stays unchanged.
    test_model_probe.py injects its own runner.
    """
    import sdlc.model_probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "_default_runner", lambda argv, timeout_s=60: (127, "hermetic")
    )


@pytest.fixture(autouse=True)
def _no_real_host_cli(monkeypatch):
    """Block real ``gh``/``glab`` invocations for every test by default.

    Under pytest the cwd is this real repository, so any dispatch-loop path
    that resolves a host adapter from the origin remote (e.g. the Story
    27.3-003 review-packet bake) would otherwise shell out to real
    ``gh pr view``/``gh pr diff`` network calls per test — the fake PR numbers
    tests use (7, 100, …) exist in this repo, so the calls even succeed. That
    slowed the suite from ~130s to 30+ minutes and tripped the dispatcher's
    300s stall watchdog. ``_default_runner`` is the adapters' designed test
    seam ("the host call is the single seam to stub"); raising from it lands
    every host-touching call site on its best-effort fallback. Local ``git``
    detection (``_remote_url``) stays real — it never leaves the machine.
    Tests that exercise adapters inject a fake runner explicitly;
    test_issue_host.py overrides this fixture to test the real runner.
    """

    def _blocked(argv, timeout=None, cwd=None, env=None):
        # ``cwd`` mirrors the real `_default_runner` (Story 32.2-002's
        # `repo_runner` passes it), so the block raises IssueHostError — the
        # error every caller degrades on — rather than a TypeError.
        raise issue_host.IssueHostError(
            f"hermetic test suite: refusing to run {argv[0]!r} — inject a fake runner"
        )

    monkeypatch.setattr(issue_host, "_default_runner", _blocked)
    # fix_issue imports the runner by value, so its module global needs the
    # same stub for its `runner or _default_runner` defaults.
    monkeypatch.setattr(fix_issue, "_default_runner", _blocked)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    """Point the host-level registry at a per-test tmp file (issue #556).

    Real-run code paths (``dispatcher=None``) in ``run_fix``/``run_build``/
    ``run_fix_batch`` instantiate ``Registry()`` with no explicit path, which
    resolves ``default_registry_path()`` — ``~/.sdlc/registry.json`` on a dev
    machine when ``SDLC_REGISTRY_PATH`` is unset. Under pytest that env var is
    unset unless a test sets it explicitly, so any test exercising a real-run
    path (e.g. ``test_batch_real_run_isolates_worktrees_and_captures_worker_exception``)
    wrote its fake run into the developer's real host registry, surfacing as a
    FAILED project card on the dashboard. ``SDLC_REGISTRY_PATH`` is the
    registry's designed test seam (registry.py); per-test
    ``monkeypatch.setenv``/``delenv`` calls in test_cli_runs.py, test_registry.py,
    and test_runlog.py run after fixture setup and override this default.
    """
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))


@pytest.fixture(autouse=True)
def _isolated_queue(monkeypatch, tmp_path):
    """Point the host-level development queue at a per-test tmp file (32.1-001).

    Mirrors ``_isolated_registry`` above: ``QueueStore(default_queue_path())``
    resolves ``~/.sdlc/queue.db`` on a dev machine when ``SDLC_QUEUE_PATH`` is
    unset, so an un-isolated test exercising `--enqueue`/`sdlc queue` would
    write real jobs into the developer's own host queue.
    """
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))


@pytest.fixture(autouse=True)
def _isolated_agent_dispatch_env(monkeypatch):
    """Strip ``SDLC_AGENT_CMD``/``SDLC_DENY_BASELINE`` for every test (issue #660).

    ``resolve_harness``/``resolve_agent_cmd`` (and ``resolve_deny_rules``) read
    these two escape-hatch vars straight from ``os.environ`` with no test seam,
    so a developer with either exported locally (e.g. testing the override
    themselves) sees the "env" harness / a different deny baseline where CI's
    clean env sees "builtin" — same ambient-env-leak class as the other
    fixtures in this file. Tests that deliberately exercise the override
    (e.g. ``test_the_env_override_slot_declares_no_floor``) call
    ``monkeypatch.setenv`` themselves, which layers on top after this fixture.
    """
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv("SDLC_DENY_BASELINE", raising=False)


@pytest.fixture(autouse=True)
def _no_real_git_push(monkeypatch):
    """Block a real ``git push`` for every test by default (issue #527).

    The bugfix loop publishes its commit with ``build._git_push`` before
    retrying the stage. On the sequential path the push root is ``workdir or
    Path.cwd()`` — and under pytest that cwd is *this* repository, where story
    ids the suite invents (28.1-002, …) collide with real local
    ``feature/<id>`` branches left by earlier `sdlc build` runs. An un-stubbed
    push therefore reaches the network, and on a checkout whose matching branch
    carries unpushed commits it would publish them to origin: running the test
    suite must never write to a remote. Same rationale, and same seam-stubbing
    shape, as :func:`_no_real_host_cli` above.

    Returns a *successful* CompletedProcess so the block is transparent — the
    loop proceeds exactly as it does when a real push lands, rather than being
    diverted into the park-on-push-failure branch. ``_push_bugfix_commit``'s own
    tests monkeypatch this same seam explicitly, which overrides the fixture.
    """
    import subprocess

    import sdlc.build as build_mod

    def _blocked_push(root, branch):
        return subprocess.CompletedProcess(
            ["git", "push", "origin", branch], 0, "", ""
        )

    monkeypatch.setattr(build_mod, "_git_push", _blocked_push)


@pytest.fixture(autouse=True)
def _green_ci_by_default(monkeypatch):
    """Give integration tests a green pipeline unless they supply a status.

    The suite refuses to run ``gh``/``glab`` (see ``_no_real_host_cli``), so every
    real CI-status lookup fails. Since issue #731 a failed lookup **blocks** the
    merge — the gate fails closed — which would stop every test that drives a
    story through its merge stage for reasons unrelated to the gate. This stub
    turns only that harness-made failure into ``success``: a status a test
    produces for real (a fake runner, a patched ``change_request_status``) is
    passed through untouched, and the gate's own tests inject ``status_fn``.
    """
    import sdlc.build as build_mod
    from sdlc import issue_host as cr_mod

    real = build_mod._ci_status_lookup

    def _lookup(ledger, story_id, pr_number):
        status = real(ledger, story_id, pr_number)
        return cr_mod.CR_SUCCESS if status is None else status

    monkeypatch.setattr(build_mod, "_ci_status_lookup", _lookup)
