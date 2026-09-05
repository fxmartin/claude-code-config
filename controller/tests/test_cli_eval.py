# ABOUTME: CLI-level tests for `sdlc eval` (Story 18.1-001) — dry-run, bad config,
# ABOUTME: and a full one-command run driven by a stub agent via $SDLC_AGENT_CMD.

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


def _write_eval_bundle(tmp_path: Path, *, n: int = 1) -> Path:
    """A self-contained config + sample target under ``tmp_path``; returns the config."""
    target = tmp_path / "sample"
    target.mkdir()
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    config = tmp_path / "eval.yaml"
    config.write_text(
        "name: cli-demo\n"
        "target: sample\n"
        f"n: {n}\n"
        "seed: 7\n"
        "tickets:\n"
        "  - id: t1\n"
        "    prompt: add a subtract function\n"
        "    quality_cmd: [\"true\"]\n",
        encoding="utf-8",
    )
    return config


def test_eval_dry_run_lists_tickets_without_dispatch(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    result = runner.invoke(app, ["eval", "--config", str(config), "--dry-run"])
    assert result.exit_code == 0
    assert "cli-demo" in result.stdout
    assert "t1" in result.stdout


def test_eval_bad_config_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\n", encoding="utf-8")  # no target / tickets
    result = runner.invoke(app, ["eval", "--config", str(bad)])
    assert result.exit_code == 2


def test_eval_rejects_n_below_one(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    result = runner.invoke(app, ["eval", "--config", str(config), "--n", "0"])
    assert result.exit_code == 2


def _write_stub_agent(tmp_path: Path) -> Path:
    """A non-streaming stub agent: edits its cwd and prints a result envelope.

    Mimics `claude -p --output-format json`: writes a new file into the working
    copy and emits a JSON envelope carrying a valid build result block plus usage
    and a notional cost, so the harness scores a real diff + tokens + cost without
    a live model.
    """
    stub = tmp_path / "stub-agent.sh"
    envelope = json.dumps(
        {
            "type": "result",
            "result": (
                "<<<RESULT_JSON>>>\n"
                + json.dumps(
                    {
                        "branch_name": "feature/eval",
                        "build_status": "SUCCESS",
                        "commit_sha": "deadbeef",
                    }
                )
                + "\n<<<END_RESULT>>>"
            ),
            "usage": {"input_tokens": 1000, "output_tokens": 200},
            "total_cost_usd": 0.07,
        }
    )
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"  # consume the prompt on stdin
        "printf 'def sub(a, b):\\n    return a - b\\n' > sub.py\n"
        f"cat <<'ENVELOPE'\n{envelope}\nENVELOPE\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return stub


def test_eval_full_run_emits_scoreboard_json(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    stub = _write_stub_agent(tmp_path)

    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(
        app, ["eval", "--config", str(config), "--json"], env=env
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["config_name"] == "cli-demo"
    overall = payload["overall"]
    assert overall["runs"] == 1
    assert overall["tokens_mean"] == 1200
    assert overall["cost_mean"] == 0.07
    assert overall["loc_added_mean"] == 2  # the two-line sub.py
    assert overall["quality_pass_rate"] == 1.0


def test_eval_n_override_changes_run_count(tmp_path: Path) -> None:
    # Config ships n=1; --n 2 overrides it, so the scoreboard reports two runs.
    config = _write_eval_bundle(tmp_path, n=1)
    stub = _write_stub_agent(tmp_path)
    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(
        app, ["eval", "--config", str(config), "--n", "2", "--json"], env=env
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["overall"]["runs"] == 2


def test_eval_full_run_table_output(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    stub = _write_stub_agent(tmp_path)
    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(app, ["eval", "--config", str(config)], env=env)
    assert result.exit_code == 0, result.stdout
    assert "eval: cli-demo" in result.stdout
    assert "OVERALL" in result.stdout


def test_eval_n_override_preserves_config_model(tmp_path: Path, monkeypatch) -> None:
    """Issue #435: the --n reconstruction of EvalConfig must carry the config's
    explicit model pin, not silently fall back to the routing default."""
    import sdlc.evaluate as evaluate_mod

    target = tmp_path / "sample"
    target.mkdir()
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    config = tmp_path / "eval.yaml"
    config.write_text(
        "name: cli-demo\n"
        "target: sample\n"
        "n: 1\n"
        "model: haiku\n"
        "tickets:\n"
        "  - id: t1\n"
        "    prompt: p\n",
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    def fake_run_eval(config, workspace, **kwargs):  # noqa: ANN001 — test double
        seen["model"] = config.model
        seen["n"] = config.n
        return []

    # eval_cmd imports run_eval from sdlc.evaluate at call time, so patching the
    # module attribute intercepts the dispatch without any live agent.
    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(app, ["eval", "--config", str(config), "--n", "2", "--json"])
    assert result.exit_code == 0, result.stdout
    assert seen == {"model": "haiku", "n": 2}


# ---------------------------------------------------------------------------
# Story 31.1-001 — --harness CLI override, precedence, scoreboard provenance,
# and preflight aborts (unknown/disabled harness, failed probe).
# ---------------------------------------------------------------------------


def test_eval_no_harness_records_claude_in_scoreboard(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    stub = _write_stub_agent(tmp_path)
    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(app, ["eval", "--config", str(config), "--json"], env=env)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["harness"] == "claude"


def test_eval_harness_flag_overrides_config(tmp_path: Path, monkeypatch) -> None:
    """The CLI flag wins over the config's `harness:` field (mirrors --model precedence)."""
    import sdlc.evaluate as evaluate_mod

    target = tmp_path / "sample"
    target.mkdir()
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    config = tmp_path / "eval.yaml"
    config.write_text(
        "name: cli-demo\ntarget: sample\nn: 1\nharness: qwen\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
        encoding="utf-8",
    )

    def fake_run_eval(config, workspace, **kwargs):  # noqa: ANN001 — test double
        return []

    # `qwen` is not in the bundled registry under this repo checkout in a way
    # that would probe cleanly here; --harness claude must win before any of
    # that is ever consulted, so run_eval only needs to be intercepted, not the
    # registry itself.
    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(
        app, ["eval", "--config", str(config), "--harness", "claude", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["harness"] == "claude"


def test_eval_config_harness_used_when_no_cli_override(tmp_path: Path, monkeypatch) -> None:
    import sdlc.evaluate as evaluate_mod

    target = tmp_path / "sample"
    target.mkdir()
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    config = tmp_path / "eval.yaml"
    config.write_text(
        "name: cli-demo\ntarget: sample\nn: 1\nharness: claude\n"
        "tickets:\n  - id: t1\n    prompt: p\n",
        encoding="utf-8",
    )

    def fake_run_eval(config, workspace, **kwargs):  # noqa: ANN001 — test double
        return []

    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(app, ["eval", "--config", str(config), "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["harness"] == "claude"


def test_eval_unknown_harness_flag_aborts_before_any_dispatch(tmp_path: Path, monkeypatch) -> None:
    import sdlc.evaluate as evaluate_mod

    config = _write_eval_bundle(tmp_path)
    called = {"run": False}

    def fake_run_eval(*a, **k):
        called["run"] = True
        return []

    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(
        app, ["eval", "--config", str(config), "--harness", "not-a-real-harness"]
    )
    assert result.exit_code == 2
    assert "not-a-real-harness" in result.stderr
    assert called["run"] is False


def test_eval_disabled_harness_aborts_before_any_dispatch(tmp_path: Path, monkeypatch) -> None:
    import sdlc.evaluate as evaluate_mod
    import sdlc.role_routing as role_routing_mod

    registry = tmp_path / "harnesses.yaml"
    registry.write_text(
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n"
        "    parser: codex-exec\n    enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(role_routing_mod, "default_registry_path", lambda: registry)

    config = _write_eval_bundle(tmp_path)
    called = {"run": False}

    def fake_run_eval(*a, **k):
        called["run"] = True
        return []

    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(app, ["eval", "--config", str(config), "--harness", "qwen"])
    assert result.exit_code == 2
    assert "disabled" in result.stderr
    assert called["run"] is False


def test_eval_failed_probe_aborts_before_any_dispatch(tmp_path: Path, monkeypatch) -> None:
    import sdlc.evaluate as evaluate_mod
    import sdlc.role_routing as role_routing_mod

    registry = tmp_path / "harnesses.yaml"
    registry.write_text(
        "harnesses:\n  qwen:\n    command: qwen-build-adapter.sh\n"
        "    parser: codex-exec\n    probe: 'no-such-qwen-binary --version'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(role_routing_mod, "default_registry_path", lambda: registry)

    config = _write_eval_bundle(tmp_path)
    called = {"run": False}

    def fake_run_eval(*a, **k):
        called["run"] = True
        return []

    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    result = runner.invoke(app, ["eval", "--config", str(config), "--harness", "qwen"])
    assert result.exit_code == 2
    assert "probe failed" in result.stderr
    assert called["run"] is False


def test_eval_json_includes_provenance_block(tmp_path: Path) -> None:
    """Story 31.1-002 AC1: every eval records a provenance block."""
    config = _write_eval_bundle(tmp_path)  # seed: 7, tickets: [t1]
    stub = _write_stub_agent(tmp_path)
    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(app, ["eval", "--config", str(config), "--json"], env=env)
    assert result.exit_code == 0, result.stdout
    prov = json.loads(result.stdout)["provenance"]
    assert prov["harness"] == "claude"
    assert prov["config_name"] == "cli-demo"
    assert prov["seed"] == 7
    assert prov["ticket_ids"] == ["t1"]
    assert prov["n"] == 1
    assert prov["model"]
    assert "/" in prov["host"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prov["timestamp"])
    # The built-in claude harness declares no `probe`, so no version is known.
    assert prov["harness_version"] is None


def test_eval_json_provenance_records_probed_harness_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry harness's declared `probe` output becomes its recorded version."""
    import sdlc.capability as capability_mod
    import sdlc.evaluate as evaluate_mod
    import sdlc.role_routing as role_routing_mod

    registry = tmp_path / "harnesses.yaml"
    registry.write_text(
        "harnesses:\n  qwen:\n    command: 'qwen-build-adapter.sh --model {model}'\n"
        "    parser: codex-exec\n    probe: 'qwen --version'\n"
        "    models:\n      default: qwen-max\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(role_routing_mod, "default_registry_path", lambda: registry)
    monkeypatch.setattr(
        capability_mod, "_default_probe_runner", lambda argv, **k: (0, "qwen version 1.2.3")
    )

    def fake_run_eval(config, workspace, **kwargs):  # noqa: ANN001 — test double
        return []

    monkeypatch.setattr(evaluate_mod, "run_eval", fake_run_eval)
    config = _write_eval_bundle(tmp_path)
    result = runner.invoke(
        app, ["eval", "--config", str(config), "--harness", "qwen", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["provenance"]["harness_version"] == "qwen version 1.2.3"


def test_eval_dry_run_still_aborts_on_unknown_harness(tmp_path: Path) -> None:
    """Preflight runs before --dry-run's listing too — no half-validated preview."""
    config = _write_eval_bundle(tmp_path)
    result = runner.invoke(
        app,
        ["eval", "--config", str(config), "--harness", "not-a-real-harness", "--dry-run"],
    )
    assert result.exit_code == 2
    assert "t1" not in result.stdout
