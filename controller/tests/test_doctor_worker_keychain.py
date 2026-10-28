# ABOUTME: `sdlc doctor`'s `worker-keychain` finding (Story 35.2-014): a Mac worker's login
# ABOUTME: Keychain must never lock on its own — read with an injected `security` runner.

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sdlc import cli, doctor

REMEDY = "security set-keychain-settings ~/Library/Keychains/login.keychain-db"


def _plist(tmp_path: Path) -> Path:
    plist = tmp_path / "w.plist"
    plist.write_bytes(b"<?xml version='1.0'?><plist version='1.0'><dict/></plist>")
    return plist


def _security(*, stderr: str = "", stdout: str = "", rc: int = 0, calls: list | None = None):
    """A `security` that answers ``show-keychain-info`` as told and records its argv."""

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    return run


def _info(settings: str, home: Path) -> str:
    # `security` writes the settings to stderr, after the quoted keychain path.
    return f'Keychain "{home}/Library/Keychains/login.keychain-db"{settings}\n'


def _check(tmp_path: Path, runner, *, system: str = "Darwin", agent: bool = True):
    home = tmp_path / "home"
    return doctor.check_worker_keychain(
        agent_path=_plist(tmp_path) if agent else tmp_path / "absent.plist",
        home=home,
        system=system,
        runner=runner,
    )


def test_a_keychain_with_no_timeout_is_clean(tmp_path) -> None:
    calls: list = []
    finding = _check(tmp_path, _security(stderr=_info(" no-timeout", tmp_path / "home"), calls=calls))

    assert (finding.check, finding.status) == ("worker-keychain", "CLEAN")
    assert "no-timeout" in finding.detail
    assert calls == [
        ["security", "show-keychain-info", str(tmp_path / "home/Library/Keychains/login.keychain-db")]
    ]


def test_an_inactivity_timeout_warns_naming_it_and_the_remedy(tmp_path) -> None:
    finding = _check(tmp_path, _security(stderr=_info(" timeout=300s", tmp_path / "home")))

    assert finding.status == "WARN"
    assert "timeout=300s" in finding.detail
    assert finding.remedy.startswith(REMEDY)


def test_lock_on_sleep_warns_even_without_a_timeout(tmp_path) -> None:
    finding = _check(tmp_path, _security(stderr=_info(" lock-on-sleep no-timeout", tmp_path / "home")))

    assert finding.status == "WARN"
    assert "lock-on-sleep" in finding.detail
    assert finding.remedy.startswith(REMEDY)


def test_both_settings_are_named(tmp_path) -> None:
    finding = _check(tmp_path, _security(stderr=_info(" lock-on-sleep timeout=21600s", tmp_path / "home")))

    assert finding.status == "WARN"
    assert "lock-on-sleep" in finding.detail and "timeout=21600s" in finding.detail


def test_settings_on_stdout_are_read_too(tmp_path) -> None:
    finding = _check(tmp_path, _security(stdout=_info(" timeout=300s", tmp_path / "home")))

    assert finding.status == "WARN"


def test_a_path_that_contains_a_setting_word_is_not_read_as_one(tmp_path) -> None:
    home = tmp_path / "lock-on-sleep timeout=1s"
    finding = doctor.check_worker_keychain(
        agent_path=_plist(tmp_path), home=home, system="Darwin",
        runner=_security(stderr=_info(" no-timeout", home)),
    )

    assert finding.status == "CLEAN"


def test_a_locked_keychain_warns_that_the_worker_cannot_reach_its_login(tmp_path) -> None:
    locked = (
        "security: SecKeychainCopySettings /Users/fx/Library/Keychains/login.keychain-db: "
        "User interaction is not allowed.\n"
    )
    finding = _check(tmp_path, _security(stderr=locked, rc=36))

    assert finding.status == "WARN"
    assert "locked" in finding.detail
    assert "Claude login" in finding.detail
    assert "security unlock-keychain" in finding.remedy


def test_any_other_failure_to_read_it_warns_never_fails(tmp_path) -> None:
    finding = _check(tmp_path, _security(stderr="security: The specified keychain could not be found.\n", rc=50))

    assert finding.status == "WARN"
    assert "could not be found" in finding.detail


def test_a_security_that_cannot_run_warns_never_fails(tmp_path) -> None:
    def missing(argv):
        raise FileNotFoundError("security")

    def hung(argv):
        raise subprocess.TimeoutExpired(argv, 10)

    for runner in (missing, hung):
        finding = _check(tmp_path, runner)
        assert finding.status == "WARN"
        assert "show-keychain-info" in finding.remedy


def test_linux_is_not_applicable(tmp_path) -> None:
    calls: list = []
    assert _check(tmp_path, _security(calls=calls), system="Linux") is None
    assert calls == []


def test_a_mac_without_the_worker_is_not_applicable(tmp_path) -> None:
    calls: list = []
    assert _check(tmp_path, _security(calls=calls), agent=False) is None
    assert calls == []


def test_run_doctor_reports_it_only_where_the_check_applies(tmp_path, monkeypatch) -> None:
    seen: list = []

    def fake(**kwargs):
        seen.append(kwargs["agent_path"])
        return doctor.Finding("worker-keychain", "Worker Keychain", "CLEAN", "no-timeout")

    monkeypatch.setattr(doctor, "check_worker_keychain", fake)
    plist = _plist(tmp_path)
    report = doctor.run_doctor(worker_plist=plist, repo_root=tmp_path)

    assert seen == [plist]
    assert any(f.check == "worker-keychain" for f in report.findings)

    monkeypatch.setattr(doctor, "check_worker_keychain", lambda **_: None)
    report = doctor.run_doctor(worker_plist=plist, repo_root=tmp_path)
    assert all(f.check != "worker-keychain" for f in report.findings)


def test_doctor_only_keeps_just_the_named_check(monkeypatch) -> None:
    findings = [
        doctor.Finding("install", "Install", "FAIL", "broken", "fix it"),
        doctor.Finding("worker-keychain", "Worker Keychain", "WARN", "timeout=300s", REMEDY),
    ]
    monkeypatch.setattr(doctor, "run_doctor", lambda **_: doctor.DoctorReport(findings=list(findings)))

    result = CliRunner().invoke(cli.app, ["doctor", "--exit-code", "--only", "worker-keychain"])

    assert result.exit_code == 1  # the WARN alone — the FAIL it left out does not count
    assert "Worker Keychain" in result.output
    assert "Install" not in result.output
