# ABOUTME: `sdlc doctor` on a Linux worker (Story 35.2-008): it finds the systemd --user unit
# ABOUTME: instead of the LaunchAgent, reads its environment, and words every remedy for systemd.

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sdlc import doctor
from sdlc import worker_selfcheck as sc
from sdlc.doctor import (
    WORKER_UNIT,
    check_fleet_worker_installed,
    check_worker_self_check,
    read_worker_unit,
    run_doctor,
)
from sdlc.queue import QueueStore
from sdlc.registry import Registry

AT = "2026-10-05T09:00:00+00:00"
NOW = datetime(2026, 10, 5, 9, 0, 0, tzinfo=timezone.utc)
HOME = Path("/home/fx")


def _unit(tmp_path: Path, body: str | None = None) -> Path:
    path = tmp_path / WORKER_UNIT
    path.write_text(
        textwrap.dedent(
            body
            or """\
            [Unit]
            Description=sdlc fleet worker dev

            [Service]
            ExecStart=/nix/var/nix/profiles/default/bin/nix develop %h/.config/nix-dev-env -c sdlc queue run --worker dev --pool claude-shared --follow
            Environment=PATH=%h/.local/bin:/usr/bin
            Environment=XDG_STATE_HOME=%h/.local/state GIT_TERMINAL_PROMPT=0
            Environment="QUOTED=two words"
            EnvironmentFile=-%h/.config/sdlc/worker.env
            """
        ),
        encoding="utf-8",
    )
    return path


# --- reading the unit -----------------------------------------------------------------


def test_the_units_environment_and_argv_are_read_with_specifiers_expanded(tmp_path) -> None:
    env, argv = read_worker_unit(_unit(tmp_path), home=HOME)

    assert env["PATH"] == "/home/fx/.local/bin:/usr/bin"
    assert env["XDG_STATE_HOME"] == "/home/fx/.local/state"  # several per line
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["QUOTED"] == "two words"
    assert argv[-5:] == ["--worker", "dev", "--pool", "claude-shared", "--follow"]


def test_the_environment_file_supplies_the_fallback_token(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".config/sdlc").mkdir(parents=True)
    (home / ".config/sdlc/worker.env").write_text(
        "# the fallback\nSDLC_QUEUE_TOKEN=s3cret\nexport OTHER='a b'\n\n", encoding="utf-8"
    )

    env, _ = read_worker_unit(_unit(tmp_path), home=home)

    assert env["SDLC_QUEUE_TOKEN"] == "s3cret"
    assert env["OTHER"] == "a b"


def test_an_absent_environment_file_is_fine_and_a_unit_is_never_a_crash(tmp_path) -> None:
    env, _ = read_worker_unit(_unit(tmp_path), home=tmp_path / "nowhere")
    assert "SDLC_QUEUE_TOKEN" not in env

    with pytest.raises(OSError):
        read_worker_unit(tmp_path / "missing.service", home=HOME)


def test_a_key_may_be_spaced_from_its_value_as_systemd_allows(tmp_path) -> None:
    unit = _unit(
        tmp_path,
        """\
        [Service]
        Environment = SDLC_QUEUE_URL=http://elsewhere:8790
        ExecStart = /usr/bin/sdlc queue run --worker dev
        """,
    )

    env, argv = read_worker_unit(unit, home=HOME)

    assert env["SDLC_QUEUE_URL"] == "http://elsewhere:8790"
    assert argv == ["/usr/bin/sdlc", "queue", "run", "--worker", "dev"]


def test_an_empty_assignment_resets_what_came_before_it(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "old.env").write_text("SDLC_QUEUE_TOKEN=old\n", encoding="utf-8")
    unit = _unit(
        tmp_path,
        """\
        [Service]
        Environment=SDLC_QUEUE_URL=http://old:8790 DROPPED=1
        EnvironmentFile=-%h/old.env
        Environment=
        EnvironmentFile=
        Environment=GIT_TERMINAL_PROMPT=0
        """,
    )

    env, _ = read_worker_unit(unit, home=home)

    assert env == {"GIT_TERMINAL_PROMPT": "0"}


def test_drop_ins_are_read_after_the_unit_in_name_order(tmp_path) -> None:
    # `systemctl --user edit sdlc-worker` writes <unit>.d/override.conf, which the
    # bootstrap's reinstall of the unit file never touches: the worker runs with it.
    unit = _unit(tmp_path)
    drop_ins = tmp_path / f"{WORKER_UNIT}.d"
    drop_ins.mkdir()
    (drop_ins / "override.conf").write_text(
        "[Service]\nEnvironment=SDLC_QUEUE_URL=http://override:8790\n"
        "ExecStart=\nExecStart=/usr/bin/sdlc queue run --worker dev2\n",
        encoding="utf-8",
    )
    (drop_ins / "10-early.conf").write_text(
        "[Service]\nEnvironment=SDLC_QUEUE_URL=http://early:8790 EARLY=1\n", encoding="utf-8"
    )
    (drop_ins / "notes.txt").write_text("[Service]\nEnvironment=IGNORED=1\n", encoding="utf-8")

    env, argv = read_worker_unit(unit, home=HOME)

    assert env["SDLC_QUEUE_URL"] == "http://override:8790"  # "override" sorts after "10-early"
    assert env["EARLY"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert "IGNORED" not in env  # only *.conf is a drop-in
    assert argv == ["/usr/bin/sdlc", "queue", "run", "--worker", "dev2"]


# --- the fleet-worker finding ---------------------------------------------------------


def _heartbeat(store: Path, *, host: str) -> None:
    queue = QueueStore(store)
    queue.init()
    queue.register_worker("dev", host=host, pools=["claude-shared"], harnesses=["claude"])


def _pinned(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "home" / ".local" / "state"
    return state, state / "sdlc" / "queue.db"


def test_a_machine_with_neither_agent_is_not_nagged(tmp_path) -> None:
    assert (
        check_fleet_worker_installed(
            agent_path=tmp_path / "x.plist", unit_path=tmp_path / "x.service", host="dev-server"
        )
        is None
    )


def test_an_installed_unit_that_has_not_registered_fails_with_a_systemd_remedy(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    # %h in the unit's EnvironmentFile= is HOME: never the real worker.env, whose
    # SDLC_QUEUE_URL (doctor's own remedy suggests one) would have this call a live queue.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    finding = check_fleet_worker_installed(
        agent_path=tmp_path / "x.plist", unit_path=_unit(tmp_path), host="dev-server"
    )

    assert finding is not None and finding.status == "FAIL"
    assert "not registered" in finding.detail
    assert "systemctl --user" in finding.remedy and "journalctl --user -u sdlc-worker" in finding.remedy
    assert "launchctl" not in finding.remedy


def test_a_unit_whose_worker_heartbeats_into_its_store_is_clean(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(_pinned(tmp_path)[0]))
    _, store = _pinned(tmp_path)
    store.parent.mkdir(parents=True)
    _heartbeat(store, host="dev-server")

    finding = check_fleet_worker_installed(
        agent_path=tmp_path / "x.plist",
        unit_path=_unit(tmp_path),
        host="dev-server",
        queue_path=store,
    )

    assert finding is not None and finding.status == "CLEAN", finding
    assert "dev" in finding.detail and "online" in finding.detail


def test_doctor_asks_the_store_a_drop_in_points_the_worker_at(tmp_path, monkeypatch) -> None:
    # Read without the drop-in, the unit pins a store nobody heartbeats into, and an
    # online worker would read as "not registered".
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = tmp_path / "moved" / "queue.db"
    store.parent.mkdir()
    _heartbeat(store, host="dev-server")
    unit = _unit(tmp_path)
    (tmp_path / f"{WORKER_UNIT}.d").mkdir()
    (tmp_path / f"{WORKER_UNIT}.d" / "override.conf").write_text(
        f"[Service]\nEnvironment=SDLC_QUEUE_PATH={store}\n", encoding="utf-8"
    )

    finding = check_fleet_worker_installed(
        agent_path=tmp_path / "x.plist", unit_path=unit, host="dev-server", queue_path=store
    )

    assert finding is not None and finding.status == "CLEAN", finding


def test_a_shell_enqueuing_to_a_fleet_queue_the_unit_does_not_drain_warns_in_systemd_terms(
    tmp_path, monkeypatch
) -> None:
    # The worker drains its own store while this shell's `--enqueue` goes to a fleet
    # service: the remedy is the fleet config the unit reads as you, never a plist.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SDLC_QUEUE_PATH", raising=False)
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://home-lab:8790")
    store = tmp_path / "queue.db"
    _heartbeat(store, host="dev-server")
    unit = _unit(tmp_path, f"[Service]\nEnvironment=SDLC_QUEUE_PATH={store}\n")

    finding = check_fleet_worker_installed(
        agent_path=tmp_path / "x.plist", unit_path=unit, host="dev-server", queue_path=store
    )

    assert finding is not None and finding.status == "WARN", finding
    assert "http://home-lab:8790" in finding.detail
    assert "the unit's environment does not carry it" in finding.detail
    assert "~/.sdlc-fleet.yaml" in finding.remedy and "plist" not in finding.remedy


def test_a_unit_that_cannot_be_read_is_a_fail_naming_the_template(tmp_path) -> None:
    broken = tmp_path / WORKER_UNIT
    broken.write_bytes(b"\xff\xfe not utf-8")

    finding = check_fleet_worker_installed(
        agent_path=tmp_path / "x.plist", unit_path=broken, host="dev-server"
    )

    assert finding is not None and finding.status == "FAIL"
    assert "templates/systemd/sdlc-worker.service" in finding.remedy


def test_the_launch_agent_still_wins_where_both_exist(tmp_path, monkeypatch) -> None:
    # A Mac has no systemd, but a stray unit must not hide the plist's finding.
    import plistlib

    plist = tmp_path / "com.fxmartin.sdlc-worker.plist"
    plist.write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": {}}))
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "q.db"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)

    finding = check_fleet_worker_installed(
        agent_path=plist, unit_path=_unit(tmp_path), host="dev-server"
    )

    assert finding is not None and "LaunchAgent" in finding.remedy


# --- the self-check finding on Linux --------------------------------------------------


def _state(tmp_path: Path, **fields) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    payload = {"ok": True, "at": AT, "reason": None, "tcc": None, "launchd": False, "systemd": True}
    payload.update(fields)
    (state / sc.STATE_FILENAME).write_text(json.dumps(payload))
    return state


def _self_check(tmp_path, state, **kwargs):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    return check_worker_self_check(
        state_dir=state, claude_dir=home / ".claude", home=home, system="Linux", **kwargs
    )


def test_a_probe_from_the_unit_is_clean_and_tcc_is_not_applicable(tmp_path) -> None:
    finding = _self_check(tmp_path, _state(tmp_path), service="systemd")

    assert (finding.check, finding.status) == ("worker-self-check", "CLEAN")
    assert "systemd unit" in finding.detail and AT in finding.detail
    assert "TCC n/a" in finding.detail


def test_a_probe_from_a_shell_is_not_clean_and_says_to_restart_the_unit(tmp_path) -> None:
    finding = _self_check(tmp_path, _state(tmp_path, systemd=False), service="systemd")

    assert finding.status == "WARN"
    assert "shell" in finding.detail and "launchd" not in finding.detail
    assert finding.remedy == "systemctl --user restart sdlc-worker"


def test_a_failed_probe_points_at_the_journal_not_at_a_mac_dialog(tmp_path) -> None:
    finding = _self_check(
        tmp_path, _state(tmp_path, ok=False, reason="agent probe failed (exit 1): not logged in"),
        service="systemd",
    )

    assert finding.status == "FAIL"
    assert "not logged in" in finding.detail
    assert "journalctl --user -u sdlc-worker" in finding.remedy
    assert "Keychain" not in finding.remedy and "Mac" not in finding.remedy


def test_no_recorded_probe_warns_with_the_systemd_restart(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    finding = _self_check(tmp_path, empty, service="systemd")

    assert finding.status == "WARN" and "no self-check recorded" in finding.detail
    assert finding.remedy == "systemctl --user restart sdlc-worker"


def test_the_default_service_is_still_launchd(tmp_path) -> None:
    finding = _self_check(tmp_path, _state(tmp_path, launchd=True, systemd=False))

    assert finding.status == "CLEAN" and "LaunchAgent" in finding.detail


# --- run_doctor on a Linux box --------------------------------------------------------


def test_run_doctor_reports_the_worker_and_its_self_check_from_the_unit(tmp_path, monkeypatch) -> None:
    from test_doctor import _healthy_install  # the shared healthy-install fixture

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    claude_dir, repo_root = _healthy_install(tmp_path)

    def report(unit: Path):
        return run_doctor(
            repo_root=repo_root,
            claude_dir=claude_dir,
            db_path=tmp_path / "ledger.db",
            queue_path=tmp_path / "queue.db",
            registry=Registry(tmp_path / "registry.json"),
            dep_probe=lambda _b: True,
            worker_plist=tmp_path / "absent.plist",
            worker_unit=unit,
        )

    checks = {f.check for f in report(tmp_path / "absent.service").findings}
    assert not {"fleet-worker", "worker-self-check", "worker-dashboard"} & checks

    found = {f.check: f for f in report(_unit(tmp_path)).findings}
    assert found["fleet-worker"].status == "FAIL"  # installed, nothing registered yet
    assert "systemctl" in found["worker-self-check"].remedy
    assert "worker-dashboard" not in found  # the Linux unit advertises no dashboard


def test_the_unit_installed_on_the_host_running_the_suite_is_never_read(tmp_path, monkeypatch) -> None:
    # `dev` runs this suite for its own story jobs with the unit installed. conftest's
    # `_no_real_worker_systemd_unit` keeps doctor off it, as its Mac twin does the plist;
    # XDG_CONFIG_HOME moves the unit, so repointing HOME alone would not hide it.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    installed = home / ".config" / "systemd" / "user" / WORKER_UNIT
    installed.parent.mkdir(parents=True)
    _unit(installed.parent)
    absent = tmp_path / "absent.plist"

    assert check_fleet_worker_installed(agent_path=absent, host="dev-server") is None
    checks = {f.check for f in run_doctor(worker_plist=absent, repo_root=tmp_path).findings}
    assert not {"fleet-worker", "worker-self-check"} & checks


def test_an_environment_file_value_with_an_unbalanced_quote_is_kept_verbatim(tmp_path) -> None:
    path = tmp_path / "worker.env"
    path.write_text("SDLC_QUEUE_TOKEN='abc\n", encoding="utf-8")
    assert doctor._environment_file(path) == {"SDLC_QUEUE_TOKEN": "'abc"}


def test_an_unquoted_environment_file_value_is_kept_whole_as_systemd_does(tmp_path) -> None:
    path = tmp_path / "worker.env"
    path.write_text('PLAIN=a  b  \nDOUBLE="c d"\nSINGLE=\'e f\'\n', encoding="utf-8")
    assert doctor._environment_file(path) == {"PLAIN": "a  b", "DOUBLE": "c d", "SINGLE": "e f"}
