# ABOUTME: CLI-level tests for `sdlc eval` (Story 18.1-001) — dry-run, bad config,
# ABOUTME: and a full one-command run driven by a stub agent via $SDLC_AGENT_CMD.

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

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


def test_eval_full_run_table_output(tmp_path: Path) -> None:
    config = _write_eval_bundle(tmp_path)
    stub = _write_stub_agent(tmp_path)
    env = dict(os.environ, SDLC_AGENT_CMD=str(stub))
    result = runner.invoke(app, ["eval", "--config", str(config)], env=env)
    assert result.exit_code == 0, result.stdout
    assert "eval: cli-demo" in result.stdout
    assert "OVERALL" in result.stdout
