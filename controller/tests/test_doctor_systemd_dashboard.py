# ABOUTME: Tests for doctor on the Linux worker (Story 35.2-010): `worker-dashboard` reads the
# ABOUTME: systemd unit's --dashboard-url, and `fleet-agent-drift` compares both units with their templates.

from __future__ import annotations

from pathlib import Path

import pytest

from sdlc.doctor import check_fleet_agent_drift, check_worker_dashboard, run_doctor

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "templates" / "systemd"
TAILNET_IP = "100.64.0.9"
WORKER_UNIT = "sdlc-worker.service"
DASHBOARD_UNIT = "sdlc-dashboard.service"


def _rendered(unit: str) -> str:
    """The unit text the bootstrap installs: the template with this box's tailnet address."""
    return (TEMPLATES / unit).read_text(encoding="utf-8").replace("__TAILNET_IP__", TAILNET_IP)


def _install(directory: Path, unit: str, text: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / unit
    path.write_text(_rendered(unit) if text is None else text, encoding="utf-8")
    return path


def _without_dashboard_url(text: str) -> str:
    """The worker unit 35.2-008 installed: its ExecStart has no --dashboard-url pair."""
    return text.replace(f" --dashboard-url http://{TAILNET_IP}:8787", "")


@pytest.fixture
def units(tmp_path: Path) -> dict[str, Path]:
    """Both units installed from their current templates."""
    unit_dir = tmp_path / "systemd" / "user"
    return {unit: _install(unit_dir, unit) for unit in (WORKER_UNIT, DASHBOARD_UNIT)}


# --- the template ------------------------------------------------------------------------


def test_the_worker_template_advertises_the_dashboard_the_dashboard_unit_binds() -> None:
    worker = (TEMPLATES / WORKER_UNIT).read_text(encoding="utf-8")
    dashboard = (TEMPLATES / DASHBOARD_UNIT).read_text(encoding="utf-8")

    assert "--dashboard-url http://__TAILNET_IP__:8787" in worker
    assert "--host __TAILNET_IP__ --port 8787" in dashboard


# --- worker-dashboard ----------------------------------------------------------------------


def test_a_unit_advertising_a_dashboard_that_answers_is_clean(units, tmp_path) -> None:
    probed: list[str] = []
    finding = check_worker_dashboard(
        agent_path=tmp_path / "absent.plist",
        unit_path=units[WORKER_UNIT],
        probe=lambda origin: probed.append(origin),
    )

    assert finding is not None and finding.status == "CLEAN"
    assert probed == [f"http://{TAILNET_IP}:8787"]


def test_a_unit_without_dashboard_url_warns_with_the_linux_bootstrap_as_remedy(tmp_path) -> None:
    unit = _install(tmp_path, WORKER_UNIT, _without_dashboard_url(_rendered(WORKER_UNIT)))

    finding = check_worker_dashboard(
        agent_path=tmp_path / "absent.plist", unit_path=unit, probe=lambda _o: None
    )

    assert finding is not None and finding.status == "WARN"
    assert "advertises no dashboard URL" in finding.detail
    assert "fleet-bootstrap.sh" in finding.remedy and "templates/systemd/" in finding.remedy
    assert "systemctl --user" in finding.remedy and "launchctl" not in finding.remedy


def test_a_dashboard_unit_that_is_not_running_warns(units, tmp_path) -> None:
    finding = check_worker_dashboard(
        agent_path=tmp_path / "absent.plist",
        unit_path=units[WORKER_UNIT],
        probe=lambda _o: "Connection refused",
    )

    assert finding is not None and finding.status == "WARN"
    assert "Connection refused" in finding.detail
    assert "fleet-bootstrap.sh" in finding.remedy and "sdlc-dashboard" in finding.remedy


def test_a_drop_in_that_replaces_exec_start_is_what_counts(units, tmp_path) -> None:
    # `systemctl --user edit` writes here; an empty ExecStart= clears the unit's before a new one.
    drop_in = units[WORKER_UNIT].with_name(f"{WORKER_UNIT}.d")
    drop_in.mkdir()
    (drop_in / "override.conf").write_text(
        "[Service]\nExecStart=\nExecStart=sdlc queue run --worker dev --follow\n", encoding="utf-8"
    )

    finding = check_worker_dashboard(
        agent_path=tmp_path / "absent.plist", unit_path=units[WORKER_UNIT], probe=lambda _o: None
    )

    assert finding is not None and finding.status == "WARN"


def test_no_worker_installed_has_no_dashboard_finding(tmp_path) -> None:
    assert (
        check_worker_dashboard(agent_path=tmp_path / "absent.plist", unit_path=tmp_path / "absent.service")
        is None
    )


# --- fleet-agent-drift ---------------------------------------------------------------------


def _drift(units: dict[str, Path]):
    return check_fleet_agent_drift(REPO_ROOT, agents={}, units=units)


def test_units_installed_from_the_current_templates_are_clean(units) -> None:
    finding = _drift(units)

    assert finding is not None and finding.status == "CLEAN"
    assert "2 installed agent(s)" in finding.detail


def test_any_tailnet_address_satisfies_the_unit_template(units) -> None:
    for path in units.values():
        path.write_text(path.read_text(encoding="utf-8").replace(TAILNET_IP, "100.101.7.42"), encoding="utf-8")

    assert _drift(units).status == "CLEAN"


def test_a_worker_unit_without_dashboard_url_is_drift_with_the_linux_bootstrap_as_remedy(
    units, monkeypatch
) -> None:
    from sdlc import doctor

    monkeypatch.setattr(doctor, "resolve_queue_url", lambda: "http://home-lab.example.ts.net:8790")
    path = units[WORKER_UNIT]
    path.write_text(_without_dashboard_url(path.read_text(encoding="utf-8")), encoding="utf-8")

    finding = _drift(units)

    assert finding.status == "WARN"
    assert finding.detail == (
        "installed agent no longer matches its template — "
        "sdlc-worker.service: missing `--dashboard-url http://<tailnet ip>:8787`"
    )
    # Linux refuses --worker / --pool: the unit is always dev on the shared pools.
    assert finding.remedy.startswith(
        f"{REPO_ROOT}/scripts/fleet-bootstrap.sh --queue-url http://home-lab.example.ts.net:8790 "
    )
    assert "--worker" not in finding.remedy and "templates/systemd/" in finding.remedy


def test_a_dashboard_unit_bound_to_every_interface_is_drift(units) -> None:
    path = units[DASHBOARD_UNIT]
    path.write_text(path.read_text(encoding="utf-8").replace(TAILNET_IP, "0.0.0.0"), encoding="utf-8")

    finding = _drift(units)

    assert finding.status == "WARN"
    assert "sdlc-dashboard.service: missing `--host <tailnet ip>`; unexpected `--host 0.0.0.0`" in finding.detail


def test_a_unit_that_is_not_installed_is_not_drift(units) -> None:
    units[DASHBOARD_UNIT].unlink()

    finding = _drift(units)

    assert finding.status == "CLEAN" and "1 installed agent(s)" in finding.detail


def test_run_doctor_on_a_linux_worker_without_dashboard_url_flags_both_checks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    unit_dir = tmp_path / "systemd" / "user"
    worker = _install(unit_dir, WORKER_UNIT, _without_dashboard_url(_rendered(WORKER_UNIT)))
    dashboard = _install(unit_dir, DASHBOARD_UNIT)

    report = run_doctor(
        repo_root=REPO_ROOT,
        claude_dir=tmp_path / ".claude",
        db_path=tmp_path / "ledger.db",
        queue_path=tmp_path / "queue.db",
        queue_service_plist=tmp_path / "absent-queue.plist",
        dashboard_plist=tmp_path / "absent-dashboard.plist",
        worker_plist=tmp_path / "absent-worker.plist",
        worker_unit=worker,
        dashboard_unit=dashboard,
        dep_probe=lambda _tool: True,
    )

    by_check = {f.check: f for f in report.findings}
    assert by_check["fleet-agent-drift"].status == "WARN"
    assert "sdlc-worker.service: missing `--dashboard-url" in by_check["fleet-agent-drift"].detail
    assert by_check["worker-dashboard"].status == "WARN"
    assert "fleet-bootstrap.sh" in by_check["worker-dashboard"].remedy


# --- the run record ----------------------------------------------------------------------


def test_the_rendered_units_exec_start_starts_a_worker_advertising_its_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What systemd runs after `nix develop … -c` is the argv whose profile stamps each run."""
    from typer.testing import CliRunner

    from sdlc import queue_worker
    from sdlc.cli import app
    from sdlc.doctor import _unit_exec_start
    from sdlc.scheduler import SchedulerResult

    argv = _unit_exec_start(_install(tmp_path, WORKER_UNIT))
    argv = argv[argv.index("-c") + 2 :]  # past `-c sdlc`
    monkeypatch.delenv("SDLC_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    detect = queue_worker.detect_worker_profile
    monkeypatch.setattr(
        "sdlc.queue_worker.detect_worker_profile",
        lambda name, **kw: detect(name, probe=lambda _b: True, runtime=lambda: None, work_dir=tmp_path, **kw),
    )
    profiles = []

    def fake_run_queue(store, **kwargs):
        profiles.append(kwargs["config"].worker)
        return SchedulerResult()

    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert profiles[0].name == "dev"
    assert profiles[0].dashboard_url == f"http://{TAILNET_IP}:8787"
