# ABOUTME: Tests for the worker's own agent self-check (Story 35.2-007): the TCC path verdict,
# ABOUTME: the 90 s probe from the worker's context, the process-group kill and the state file.

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sdlc import worker_selfcheck as sc
from sdlc.model_routing import TIER_MODEL_IDS

NOW = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    return home, claude


# --- TCC path verdict ---------------------------------------------------------


def test_a_claude_dir_outside_protected_folders_has_no_verdict(tmp_path) -> None:
    home, claude = _home(tmp_path)
    (claude / "settings.json").write_text("{}")
    assert sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin") is None


@pytest.mark.parametrize(
    "root",
    [
        "Documents",
        "Desktop",
        "Downloads",
        "Library/Mobile Documents",
        "Library/CloudStorage",
        "Dropbox",
        "Google Drive",
        "OneDrive - Acme",
    ],
)
def test_an_entry_resolving_into_a_protected_root_fails_fast(tmp_path, root) -> None:
    home, claude = _home(tmp_path)
    target = home / root / "nix-install" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}")
    (claude / "settings.json").symlink_to(target)

    verdict = sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin")

    assert verdict == (
        f"~/.claude/settings.json → ~/{root}/…: launchd agents cannot read TCC protected "
        "folders; move the checkout (nix-install: ~/.config/nix-install)"
    )


@pytest.mark.parametrize(
    "entry", ["settings.json", "CLAUDE.md", "hooks", "commands", "agents", "skills"]
)
def test_every_named_claude_entry_is_checked(tmp_path, entry) -> None:
    home, claude = _home(tmp_path)
    target = home / "Documents" / "cfg" / entry
    target.parent.mkdir(parents=True)
    target.touch()
    (claude / entry).symlink_to(target)

    verdict = sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin")

    assert verdict is not None and verdict.startswith(f"~/.claude/{entry} → ~/Documents/…")


def test_a_claude_dir_that_is_itself_a_symlink_into_documents_is_named(tmp_path) -> None:
    home = tmp_path / "home"
    real = home / "Documents" / "claude-code-config"
    real.mkdir(parents=True)
    claude = home / ".claude"
    claude.symlink_to(real)

    verdict = sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin")

    assert verdict is not None and verdict.startswith("~/.claude → ~/Documents/…")


def test_a_dangling_symlink_into_documents_is_still_a_verdict(tmp_path) -> None:
    home, claude = _home(tmp_path)
    (claude / "hooks").symlink_to(home / "Documents" / "gone")
    assert sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin") is not None


def test_a_symlinked_home_does_not_hide_a_protected_target(tmp_path) -> None:
    real_home = tmp_path / "real-home"
    (real_home / "Documents" / "cfg").mkdir(parents=True)
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home)
    claude = link_home / ".claude"
    claude.mkdir()
    (claude / "agents").symlink_to(real_home / "Documents" / "cfg")

    verdict = sc.tcc_verdict(claude_dir=claude, home=link_home, system="Darwin")

    assert verdict is not None and "~/.claude/agents" in verdict


def test_a_similarly_named_sibling_is_not_protected(tmp_path) -> None:
    home, claude = _home(tmp_path)
    target = home / "Documents-archive" / "cfg"
    target.mkdir(parents=True)
    (claude / "agents").symlink_to(target)
    assert sc.tcc_verdict(claude_dir=claude, home=home, system="Darwin") is None


def test_the_tcc_check_is_skipped_off_macos(tmp_path) -> None:
    home, claude = _home(tmp_path)
    target = home / "Documents" / "cfg"
    target.mkdir(parents=True)
    (claude / "agents").symlink_to(target)
    assert sc.tcc_verdict(claude_dir=claude, home=home, system="Linux") is None


# --- the agent probe ----------------------------------------------------------


def _runner(code: int, detail: str = "", calls: list | None = None):
    def run(argv, timeout_s=60):
        if calls is not None:
            calls.append((list(argv), timeout_s))
        return code, detail

    return run


def test_the_probe_is_the_haiku_entitlement_probe_with_a_90s_cap(tmp_path) -> None:
    home, claude = _home(tmp_path)
    calls: list = []

    result = sc.run_self_check(
        runner=_runner(0, '{"result":"ok"}', calls),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )

    assert result.ok and result.reason is None
    assert calls == [
        (
            ["claude", "-p", "ok", "--model", TIER_MODEL_IDS["haiku"], "--output-format", "json"],
            90,
        )
    ]


def test_a_stalled_probe_names_the_dialog_and_the_cap(tmp_path) -> None:
    home, claude = _home(tmp_path)

    result = sc.run_self_check(
        runner=_runner(124, "probe command timed out"),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )

    assert not result.ok
    assert result.reason == (
        "agent produced no output in 90s — a dialog is probably waiting on this Mac "
        "(Keychain / Privacy & Security) or ~/.claude resolves into a protected folder"
    )


def test_a_missing_cli_is_a_failure_that_says_so(tmp_path) -> None:
    home, claude = _home(tmp_path)
    result = sc.run_self_check(
        runner=_runner(127, "command not found: claude"),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )
    assert not result.ok and "claude" in (result.reason or "") and "PATH" in (result.reason or "")


def test_an_erroring_agent_is_a_failure_with_its_first_line(tmp_path) -> None:
    home, claude = _home(tmp_path)
    result = sc.run_self_check(
        runner=_runner(1, "Invalid API key · Please run /login\nmore"),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )
    assert not result.ok
    assert result.reason == "agent probe failed (exit 1): Invalid API key · Please run /login"


def test_a_rate_limited_agent_did_run_a_turn(tmp_path) -> None:
    # The rate-limit window is the queue's to wait out; it is not a stalled agent.
    home, claude = _home(tmp_path)
    result = sc.run_self_check(
        runner=_runner(1, "API Error: 429 rate_limit_error: too many requests"),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )
    assert result.ok


def test_a_tcc_verdict_fails_before_any_probe_runs(tmp_path) -> None:
    home, claude = _home(tmp_path)
    target = home / "Documents" / "cfg"
    target.mkdir(parents=True)
    (claude / "settings.json").symlink_to(target)
    calls: list = []

    result = sc.run_self_check(
        runner=_runner(0, "", calls),
        claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )

    assert not result.ok and result.reason.startswith("~/.claude/settings.json → ~/Documents/…")
    assert calls == []


def test_a_broken_runner_is_a_failure_not_a_crash(tmp_path) -> None:
    home, claude = _home(tmp_path)

    def boom(argv, timeout_s=60):
        raise OSError("spawn failed")

    result = sc.run_self_check(
        runner=boom, claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=tmp_path / "state", environ={},
    )
    assert not result.ok and "spawn failed" in (result.reason or "")


# --- the result: heartbeat shape and the state file ---------------------------


def test_the_heartbeat_payload_is_ok_at_reason(tmp_path) -> None:
    home, claude = _home(tmp_path)
    result = sc.run_self_check(
        runner=_runner(0), claude_dir=claude, home=home, system="Darwin",
        clock=lambda: NOW, state_dir=tmp_path / "state", environ={},
    )
    assert result.to_heartbeat() == {"ok": True, "at": NOW.isoformat(), "reason": None}


def test_the_result_is_recorded_for_doctor_with_the_context_it_ran_in(tmp_path) -> None:
    home, claude = _home(tmp_path)
    state = tmp_path / "state"

    sc.run_self_check(
        runner=_runner(0), claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=state, environ={"XPC_SERVICE_NAME": "com.fxmartin.sdlc-worker"},
    )

    last = sc.read_last_result(state)
    assert last == {
        "ok": True, "at": NOW.isoformat(), "reason": None, "tcc": None, "launchd": True,
    }


@pytest.mark.parametrize(
    ("xpc", "launchd"),
    [
        (None, False),
        ("0", False),
        ("", False),
        ("application.com.googlecode.iterm2.1234", False),
        ("com.fxmartin.sdlc-worker", True),
    ],
)
def test_only_a_launchd_job_counts_as_launchd(xpc, launchd) -> None:
    env = {} if xpc is None else {"XPC_SERVICE_NAME": xpc}
    assert sc.running_under_launchd(env) is launchd


def test_an_unwritable_state_dir_does_not_fail_the_check(tmp_path) -> None:
    home, claude = _home(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    result = sc.run_self_check(
        runner=_runner(0), claude_dir=claude, home=home, system="Darwin", clock=lambda: NOW,
        state_dir=blocker / "state", environ={},
    )
    assert result.ok


@pytest.mark.parametrize("content", [None, "not json", "[1]", '{"ok": "yes"}'])
def test_an_absent_or_junk_state_file_reads_as_none(tmp_path, content) -> None:
    state = tmp_path / "state"
    state.mkdir()
    if content is not None:
        (state / sc.STATE_FILENAME).write_text(content)
    assert sc.read_last_result(state) is None


# --- the process-group runner -------------------------------------------------


def test_the_group_runner_returns_output_of_a_finished_process() -> None:
    code, detail = sc.group_runner([sys.executable, "-c", "print('hi')"], timeout_s=30)
    assert (code, detail) == (0, "hi")


def test_the_group_runner_reports_a_missing_binary() -> None:
    assert sc.group_runner(["definitely-not-a-binary-xyz"], timeout_s=5) == (
        127, "command not found: definitely-not-a-binary-xyz",
    )


def test_the_group_runner_kills_the_whole_process_group_on_timeout(tmp_path) -> None:
    """A stalled agent's children die with it — no orphan keeps a dialog open."""
    beat = tmp_path / "child.beat"
    child_src = (
        "import pathlib, time\n"
        "while True:\n"
        f"    pathlib.Path({str(beat)!r}).write_text(str(time.monotonic()))\n"
        "    time.sleep(0.05)\n"
    )
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_src!r}])\n"
        "time.sleep(120)\n"
    )

    code, detail = sc.group_runner([sys.executable, "-c", script], timeout_s=3)

    assert (code, detail) == (124, "probe command timed out")
    # A PID-1 `sh` never reaps, so liveness is read from the child's own heartbeat:
    # it must go quiet (a zombie writes nothing), with headroom for a loaded host.
    deadline = time.monotonic() + 20
    quiet_since = None
    last = beat.read_text() if beat.exists() else None
    while time.monotonic() < deadline:
        time.sleep(0.25)
        now = beat.read_text() if beat.exists() else None
        if now != last:
            last, quiet_since = now, None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= 1.0:
            return
    pytest.fail("the probe's child kept running after its process group was killed")
