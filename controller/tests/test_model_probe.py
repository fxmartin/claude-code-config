# ABOUTME: Tests for the live model entitlement probe + previous-generation fallback (Story 34.1-002).
# ABOUTME: The probe runner is always injected; no test ever calls the claude CLI.

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sdlc import model_probe, model_routing
from sdlc.dispatch import resolve_agent_cmd
from sdlc.model_routing import (
    OPUS,
    TIER_FALLBACK_IDS,
    TIER_MODEL_IDS,
    resolve_model_id,
    routing_banner,
)

# conftest swaps the module attribute per test; keep the real function for its own tests.
REAL_DEFAULT_RUNNER = model_probe._default_runner
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
NOT_FOUND = (1, "API Error: 404 not_found_error: model: claude-opus-5-5")
RATE_LIMITED = (1, "API Error: 429 rate_limit_error: too many requests")


@pytest.fixture(autouse=True)
def _reset_state():
    model_routing.reset_probe_state()
    yield
    model_routing.reset_probe_state()


def _runner(results, calls=None):
    def run(argv, timeout_s=60):
        if calls is not None:
            calls.append(argv)
        model = argv[argv.index("--model") + 1]
        return results.get(model, (0, "ok"))

    return run


def test_fallback_table_matches_spec():
    assert TIER_FALLBACK_IDS == {
        "claude-opus-5-5": "claude-opus-5",
        "claude-sonnet-5-5": "claude-sonnet-5",
        "claude-haiku-4-5": "claude-haiku-4-5",
    }


def test_entitlement_failure_substitutes_previous_generation(tmp_path):
    outcome = model_probe.probe_tier_models(
        runner=_runner({TIER_MODEL_IDS[OPUS]: NOT_FOUND}),
        state_dir=tmp_path, now=NOW,
    )
    assert outcome.substitutions == {TIER_MODEL_IDS[OPUS]: "claude-opus-5"}
    assert resolve_model_id(OPUS) == "claude-opus-5"
    assert any("claude-opus-5-5" in w and "claude-opus-5" in w for w in outcome.warnings)
    assert "claude-opus-5" in " ".join(routing_banner({"profile": "balanced", "map": {}}) + [
        model_routing._with_model_id(OPUS)
    ])
    assert "unavailable" in model_routing._with_model_id(OPUS)


def test_success_adds_fallback_model_flag(tmp_path):
    model_probe.probe_tier_models(runner=_runner({}), state_dir=tmp_path, now=NOW)
    cmd = resolve_agent_cmd(model=resolve_model_id(OPUS))
    i = cmd.index("--fallback-model")
    assert cmd[i + 1] == "claude-opus-5"


def test_no_fallback_flag_without_probe_or_when_substituted(tmp_path):
    assert "--fallback-model" not in resolve_agent_cmd(model=resolve_model_id(OPUS))
    model_probe.probe_tier_models(
        runner=_runner({TIER_MODEL_IDS[OPUS]: NOT_FOUND}), state_dir=tmp_path, now=NOW
    )
    assert "--fallback-model" not in resolve_agent_cmd(model=resolve_model_id(OPUS))


def test_rate_limit_is_not_an_entitlement_failure(tmp_path):
    outcome = model_probe.probe_tier_models(
        runner=_runner({TIER_MODEL_IDS[OPUS]: RATE_LIMITED}),
        state_dir=tmp_path, now=NOW,
    )
    assert outcome.substitutions == {}
    assert resolve_model_id(OPUS) == TIER_MODEL_IDS[OPUS]
    assert "--fallback-model" not in resolve_agent_cmd(model=resolve_model_id(OPUS))


def test_classify():
    assert model_probe.classify_probe(0, "ok") == "ok"
    assert model_probe.classify_probe(1, NOT_FOUND[1]) == "entitlement"
    assert model_probe.classify_probe(1, "There's an issue with the selected model") == "entitlement"
    assert model_probe.classify_probe(1, RATE_LIMITED[1]) == "inconclusive"
    assert model_probe.classify_probe(124, "probe command timed out") == "inconclusive"


def test_result_cached_per_host_for_24h(tmp_path):
    calls: list = []
    run = _runner({TIER_MODEL_IDS[OPUS]: NOT_FOUND}, calls)
    model_probe.probe_tier_models(runner=run, state_dir=tmp_path, now=NOW)
    n = len(calls)
    assert n >= 1
    model_routing.reset_probe_state()
    out = model_probe.probe_tier_models(
        runner=run, state_dir=tmp_path, now=NOW + timedelta(hours=23)
    )
    assert len(calls) == n
    assert out.substitutions == {TIER_MODEL_IDS[OPUS]: "claude-opus-5"}
    model_routing.reset_probe_state()
    model_probe.probe_tier_models(
        runner=run, state_dir=tmp_path, now=NOW + timedelta(hours=25)
    )
    assert len(calls) == 2 * n


def test_cache_is_keyed_by_host(tmp_path):
    model_probe.probe_tier_models(runner=_runner({}), state_dir=tmp_path, now=NOW, host="a")
    data = json.loads((tmp_path / model_probe.CACHE_FILENAME).read_text())
    assert "a" in data
    calls: list = []
    model_probe.probe_tier_models(runner=_runner({}, calls), state_dir=tmp_path, now=NOW, host="b")
    assert calls


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / model_probe.CACHE_FILENAME).write_text("{not json")
    calls: list = []
    model_probe.probe_tier_models(runner=_runner({}, calls), state_dir=tmp_path, now=NOW)
    assert calls


def test_runner_exception_is_inconclusive(tmp_path):
    def boom(argv, timeout_s=60):
        raise OSError("no claude")

    out = model_probe.probe_tier_models(runner=boom, state_dir=tmp_path, now=NOW)
    assert out.substitutions == {}
    assert resolve_model_id(OPUS) == TIER_MODEL_IDS[OPUS]


def test_doctor_finding_lists_each_tier(tmp_path):
    from sdlc.doctor import check_model_probe

    f = check_model_probe(
        runner=_runner({TIER_MODEL_IDS[OPUS]: NOT_FOUND}), state_dir=tmp_path, now=NOW
    )
    assert f.check == "model-probe"
    assert f.status == "WARN"
    for tier, mid in TIER_MODEL_IDS.items():
        assert mid in f.detail
    assert "unavailable" in f.detail and "ok" in f.detail

    clean = check_model_probe(runner=_runner({}), state_dir=tmp_path / "x", now=NOW)
    assert clean.status == "CLEAN"


def test_default_runner_maps_subprocess_outcomes(monkeypatch):
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(model_probe.subprocess, "run", missing)
    assert REAL_DEFAULT_RUNNER(["claude"]) == (127, "command not found: claude")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(model_probe.subprocess, "run", slow)
    assert REAL_DEFAULT_RUNNER(["claude"]) == (124, "probe command timed out")

    done = subprocess.CompletedProcess(["claude"], 1, stdout="out", stderr="")
    monkeypatch.setattr(model_probe.subprocess, "run", lambda *a, **k: done)
    assert REAL_DEFAULT_RUNNER(["claude"]) == (1, "out")


def test_default_state_dir_is_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(model_probe.Path, "home", classmethod(lambda cls: tmp_path))
    assert model_probe.default_state_dir() == tmp_path / ".local" / "state" / "sdlc"


def test_store_cache_merges_and_recovers_from_non_dict(tmp_path):
    path = tmp_path / "sub" / model_probe.CACHE_FILENAME
    model_probe._store_cache(path, "a", NOW, {"m": "ok"})
    model_probe._store_cache(path, "b", NOW, {"m": "ok"})
    assert set(json.loads(path.read_text())) == {"a", "b"}
    path.write_text("[1, 2]")
    model_probe._store_cache(path, "c", NOW, {"m": "ok"})
    assert set(json.loads(path.read_text())) == {"c"}


def test_store_cache_swallows_write_errors(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # parent is a regular file: mkdir fails regardless of euid
    model_probe._store_cache(blocker / "x" / "c.json", "a", NOW, {"m": "ok"})
