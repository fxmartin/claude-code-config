# ABOUTME: Tests for doctor's `fleet-agent-drift` check (Story 35.2-009): an installed worker,
# ABOUTME: dashboard or queue LaunchAgent whose arguments differ from what its template renders.

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from sdlc.doctor import check_fleet_agent_drift, run_doctor

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "templates" / "launchd"
HOME = Path("/Users/fx")
TAILNET_IP = "100.64.0.9"


def _rendered_args(label: str, *, worker: str = "m3max", pools: tuple[str, ...] = ("claude-m3", "codex-shared")) -> list[str]:
    """The ProgramArguments the bootstrap installs for ``label`` on a Mac at ``HOME``."""
    with (TEMPLATES / f"{label}.plist").open("rb") as fh:
        args = [str(a) for a in plistlib.load(fh)["ProgramArguments"]]
    args = [a.replace("__HOME__", str(HOME)).replace("__TAILNET_IP__", TAILNET_IP) for a in args]
    if "--worker" in args:
        i = args.index("--worker")
        args[i + 1] = worker
        first = args.index("--pool")
        last = len(args) - 1 - args[::-1].index("--pool") + 1
        args[first : last + 1] = [x for p in pools for x in ("--pool", p)]
    return args


def _install(tmp_path: Path, label: str, args: list[str]) -> Path:
    path = tmp_path / f"{label}.plist"
    path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args}))
    return path


@pytest.fixture
def agents(tmp_path: Path) -> dict[str, Path]:
    """Every agent installed from its current template."""
    return {
        label: _install(tmp_path, label, _rendered_args(label))
        for label in ("com.fxmartin.sdlc-worker", "com.fxmartin.sdlc-dashboard", "com.fxmartin.sdlc-queue")
    }


def _check(agents: dict[str, Path]):
    return check_fleet_agent_drift(REPO_ROOT, agents=agents, home=HOME)


def test_agents_installed_from_the_current_templates_are_clean(agents) -> None:
    finding = _check(agents)

    assert finding is not None
    assert finding.check == "fleet-agent-drift"
    assert finding.status == "CLEAN"


def test_a_worker_with_no_dashboard_url_warns_naming_agent_and_argument(agents, tmp_path) -> None:
    args = _rendered_args("com.fxmartin.sdlc-worker")
    i = args.index("--dashboard-url")
    agents["com.fxmartin.sdlc-worker"] = _install(tmp_path, "com.fxmartin.sdlc-worker", args[:i] + args[i + 2 :])

    finding = _check(agents)

    assert finding.status == "WARN"
    assert "com.fxmartin.sdlc-worker" in finding.detail
    assert "--dashboard-url" in finding.detail
    assert "fleet-bootstrap.sh" in finding.remedy


def test_the_remedy_repeats_the_workers_own_name_and_pools(agents, tmp_path) -> None:
    args = _rendered_args("com.fxmartin.sdlc-worker", worker="home-lab", pools=("claude-shared",))
    del args[args.index("--dashboard-url") : args.index("--dashboard-url") + 2]
    agents["com.fxmartin.sdlc-worker"] = _install(tmp_path, "com.fxmartin.sdlc-worker", args)

    finding = _check(agents)

    assert "--worker home-lab" in finding.remedy
    assert "--pool claude-shared" in finding.remedy
    assert "claude-m3" not in finding.remedy


def test_the_hosts_own_worker_name_and_pools_are_not_drift(tmp_path) -> None:
    # The template carries the M3's name and pools; another host's choices are its own.
    path = _install(tmp_path, "com.fxmartin.sdlc-worker", _rendered_args("com.fxmartin.sdlc-worker", worker="dev", pools=("claude-shared",)))

    finding = _check({"com.fxmartin.sdlc-worker": path})

    assert finding.status == "CLEAN"


def test_a_dashboard_bound_to_every_interface_is_different_not_missing(agents, tmp_path) -> None:
    args = _rendered_args("com.fxmartin.sdlc-dashboard")
    args[args.index("--host") + 1] = "0.0.0.0"
    agents["com.fxmartin.sdlc-dashboard"] = _install(tmp_path, "com.fxmartin.sdlc-dashboard", args)

    finding = _check(agents)

    assert finding.status == "WARN"
    assert "com.fxmartin.sdlc-dashboard" in finding.detail
    assert "--host 0.0.0.0" in finding.detail  # what is installed
    assert "--host" in finding.detail and "<tailnet ip>" in finding.detail  # what it should be


def test_a_queue_service_on_another_port_warns(agents, tmp_path) -> None:
    args = _rendered_args("com.fxmartin.sdlc-queue")
    args[args.index("--bind") + 1] = f"{TAILNET_IP}:9999"
    agents["com.fxmartin.sdlc-queue"] = _install(tmp_path, "com.fxmartin.sdlc-queue", args)

    finding = _check(agents)

    assert finding.status == "WARN"
    assert "com.fxmartin.sdlc-queue" in finding.detail
    assert "--queue-service" in finding.remedy


def test_any_tailnet_address_satisfies_the_template(agents, tmp_path) -> None:
    # Doctor never asks tailscale: a tailnet address is the template's "value", whichever it is.
    args = [a.replace(TAILNET_IP, "100.101.102.103") for a in _rendered_args("com.fxmartin.sdlc-worker")]
    agents["com.fxmartin.sdlc-worker"] = _install(tmp_path, "com.fxmartin.sdlc-worker", args)

    assert _check(agents).status == "CLEAN"


def test_an_agent_that_is_not_installed_is_not_drift(agents, tmp_path) -> None:
    agents["com.fxmartin.sdlc-queue"] = tmp_path / "absent.plist"

    assert _check(agents).status == "CLEAN"


def test_a_host_with_no_agents_has_nothing_to_report(tmp_path) -> None:
    assert check_fleet_agent_drift(REPO_ROOT, agents={"com.fxmartin.sdlc-worker": tmp_path / "absent.plist"}, home=HOME) is None


def test_a_checkout_without_templates_has_nothing_to_compare(agents, tmp_path) -> None:
    assert check_fleet_agent_drift(tmp_path, agents=agents, home=HOME) is None


def test_an_unreadable_plist_is_left_to_the_installed_checks(agents, tmp_path) -> None:
    broken = tmp_path / "broken.plist"
    broken.write_text("not a plist", encoding="utf-8")
    agents["com.fxmartin.sdlc-worker"] = broken

    assert _check(agents).status == "CLEAN"


def test_run_doctor_reports_drift_for_the_installed_worker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _rendered_args("com.fxmartin.sdlc-worker")
    del args[args.index("--dashboard-url") : args.index("--dashboard-url") + 2]
    worker = _install(tmp_path, "com.fxmartin.sdlc-worker", args)

    report = run_doctor(
        repo_root=REPO_ROOT,
        claude_dir=tmp_path / ".claude",
        db_path=tmp_path / "ledger.db",
        queue_path=tmp_path / "queue.db",
        queue_service_plist=tmp_path / "absent-queue.plist",
        dashboard_plist=tmp_path / "absent-dashboard.plist",
        worker_plist=worker,
        worker_unit=tmp_path / "absent.service",
        dep_probe=lambda _tool: True,
    )

    drift = [f for f in report.findings if f.check == "fleet-agent-drift"]
    assert len(drift) == 1 and drift[0].status == "WARN"


def test_the_remedy_falls_back_to_a_placeholder_when_no_queue_url_resolves(agents, tmp_path, monkeypatch) -> None:
    from sdlc import doctor
    from sdlc.queue import QueueError

    def _unresolvable() -> str:
        raise QueueError("no queue url")

    monkeypatch.setattr(doctor, "resolve_queue_url", _unresolvable)
    args = _rendered_args("com.fxmartin.sdlc-worker")
    del args[args.index("--dashboard-url") : args.index("--dashboard-url") + 2]
    agents["com.fxmartin.sdlc-worker"] = _install(tmp_path, "com.fxmartin.sdlc-worker", args)

    finding = _check(agents)

    assert finding.status == "WARN"
    assert "--queue-url URL" in finding.remedy
