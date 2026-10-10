# ABOUTME: `sdlc doctor`'s `worker-self-check` finding (Story 35.2-007): last probe result,
# ABOUTME: its timestamp and the TCC path verdict — CLEAN only for a probe run by the LaunchAgent.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdlc import doctor
from sdlc.worker_selfcheck import STATE_FILENAME

AT = "2026-10-04T09:00:00+00:00"


def _state(tmp_path: Path, **fields) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    payload = {"ok": True, "at": AT, "reason": None, "tcc": None, "launchd": True}
    payload.update(fields)
    (state / STATE_FILENAME).write_text(json.dumps(payload))
    return state


def _claude(tmp_path: Path, *, protected: bool = False) -> tuple[Path, Path]:
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    if protected:
        target = home / "Documents" / "cfg"
        target.mkdir(parents=True)
        (claude / "settings.json").symlink_to(target)
    return home, claude


def _check(tmp_path, state, *, protected=False, system="Darwin"):
    home, claude = _claude(tmp_path, protected=protected)
    return doctor.check_worker_self_check(
        state_dir=state, claude_dir=claude, home=home, system=system
    )


def test_a_probe_that_completed_from_the_launch_agent_is_clean(tmp_path) -> None:
    finding = _check(tmp_path, _state(tmp_path))

    assert (finding.check, finding.status) == ("worker-self-check", "CLEAN")
    assert AT in finding.detail
    assert "LaunchAgent" in finding.detail
    assert "TCC" in finding.detail


def test_a_probe_that_completed_from_a_shell_is_not_clean(tmp_path) -> None:
    finding = _check(tmp_path, _state(tmp_path, launchd=False))

    assert finding.status == "WARN"
    assert "shell" in finding.detail and AT in finding.detail
    assert "launchctl kickstart" in finding.remedy


def test_a_failed_probe_is_a_fail_with_its_reason_and_time(tmp_path) -> None:
    finding = _check(tmp_path, _state(tmp_path, ok=False, reason="agent produced no output in 90s"))

    assert finding.status == "FAIL"
    assert "agent produced no output in 90s" in finding.detail and AT in finding.detail


def test_no_recorded_probe_is_a_warn(tmp_path) -> None:
    finding = _check(tmp_path, tmp_path / "empty-state")

    assert finding.status == "WARN"
    assert "no self-check recorded" in finding.detail


def test_a_tcc_protected_claude_dir_fails_even_after_a_good_probe(tmp_path) -> None:
    finding = _check(tmp_path, _state(tmp_path), protected=True)

    assert finding.status == "FAIL"
    assert "~/.claude/settings.json → ~/Documents/…" in finding.detail


def test_the_tcc_verdict_is_not_applied_off_macos(tmp_path) -> None:
    finding = _check(tmp_path, _state(tmp_path), protected=True, system="Linux")

    assert finding.status == "CLEAN"
    assert "not macOS" in finding.detail


def test_run_doctor_reports_it_only_on_a_machine_with_the_worker_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        doctor, "check_worker_self_check",
        lambda **_: doctor.Finding("worker-self-check", "Worker self-check", "CLEAN", "x"),
    )
    without = doctor.run_doctor(worker_plist=tmp_path / "absent.plist", repo_root=tmp_path)
    assert all(f.check != "worker-self-check" for f in without.findings)

    plist = tmp_path / "w.plist"
    plist.write_bytes(b"<?xml version='1.0'?><plist version='1.0'><dict/></plist>")
    with_agent = doctor.run_doctor(worker_plist=plist, repo_root=tmp_path)
    assert any(f.check == "worker-self-check" for f in with_agent.findings)
