# ABOUTME: Unit tests for the queue's ordering/overlap/budget policy (Story 32.3-001).
# ABOUTME: Pure functions over queue rows + investigation output — no scheduler involved.

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


def _store(tmp_path):
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _now() -> datetime:
    return datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


# --- AC1: priority classes mirroring `fix all`'s order ---------------------


def test_default_priority_puts_fix_above_build() -> None:
    """An unlabelled `fix` job outranks a `build` job — AC1's default rule."""
    from sdlc.queue import PRIORITY_CLASSES, default_priority

    fix = default_priority("fix")
    build = default_priority("build")
    assert PRIORITY_CLASSES.index(fix) > PRIORITY_CLASSES.index(build)


def test_default_priority_puts_bugs_above_enhancements() -> None:
    """`bug` outranks `enhancement`, mirroring `fix all`'s category order."""
    from sdlc.queue import PRIORITY_CLASSES, default_priority

    bug = default_priority("fix", labels=["bug"])
    enhancement = default_priority("fix", labels=["enhancement"])
    assert PRIORITY_CLASSES.index(bug) > PRIORITY_CLASSES.index(enhancement)


def test_default_priority_matches_fix_alls_label_vocabulary() -> None:
    """Reuses `fix all`'s own predicates — `feature` counts as an enhancement."""
    from sdlc.queue import default_priority

    assert default_priority("fix", labels=["type/Bug"]) == default_priority(
        "fix", labels=["bug"]
    )
    assert default_priority("fix", labels=["feature"]) == default_priority(
        "fix", labels=["enhancement"]
    )


def test_add_job_derives_priority_from_kind(tmp_path) -> None:
    store = _store(tmp_path)
    fix_id = store.add_job(repo="/a", kind="fix", scope="42")
    build_id = store.add_job(repo="/b", kind="build", scope="epic-3")

    from sdlc.queue import PRIORITY_CLASSES

    fix = store.get_job(fix_id)
    build = store.get_job(build_id)
    assert PRIORITY_CLASSES.index(fix.priority) > PRIORITY_CLASSES.index(build.priority)


def test_add_job_explicit_priority_still_wins(tmp_path) -> None:
    """An operator's explicit class is never overwritten by the derivation."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42", priority="low")
    assert store.get_job(job_id).priority == "low"


def test_claim_order_is_priority_class_then_age(tmp_path) -> None:
    """AC1: class first, then age (FIFO) inside a class."""
    store = _store(tmp_path)
    old_normal = store.add_job(repo="/a", kind="build", scope="1", priority="normal")
    new_normal = store.add_job(repo="/b", kind="build", scope="2", priority="normal")
    urgent = store.add_job(repo="/c", kind="fix", scope="3", priority="urgent")

    assert [j.id for j in store.peek_claimable(now=_now())] == [
        urgent, old_normal, new_normal,
    ]


def test_prioritise_moves_a_job_between_classes(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.add_job(repo="/a", kind="build", scope="1")
    second = store.add_job(repo="/b", kind="build", scope="2")
    assert [j.id for j in store.peek_claimable(now=_now())] == [first, second]

    store.prioritise_job(second, "urgent")
    assert [j.id for j in store.peek_claimable(now=_now())] == [second, first]


# --- AC2: repo-scoped overlap serialisation --------------------------------


def test_overlap_dependencies_chains_jobs_sharing_a_file() -> None:
    """Two jobs in one repo touching a common file serialise, oldest first."""
    from sdlc.queue import overlap_dependencies

    rows = [(1, "/repo"), (2, "/repo"), (3, "/repo")]
    files = {1: {"src/a.py"}, 2: {"src/a.py"}, 3: {"src/z.py"}}
    assert overlap_dependencies(rows, files) == {1: [], 2: [1], 3: []}


def test_overlap_dependencies_are_scoped_to_one_repo() -> None:
    """Jobs in different repos never serialise, however much they overlap."""
    from sdlc.queue import overlap_dependencies

    rows = [(1, "/alpha"), (2, "/beta")]
    files = {1: {"src/a.py"}, 2: {"src/a.py"}}
    assert overlap_dependencies(rows, files) == {1: [], 2: []}


def test_overlap_dependencies_normalise_equivalent_paths() -> None:
    """`./src/a.py` and `src/a.py` are the same file — inherited from #436."""
    from sdlc.queue import overlap_dependencies

    rows = [(1, "/repo"), (2, "/repo")]
    files = {1: {"./src/a.py"}, 2: {"src/a.py"}}
    assert overlap_dependencies(rows, files) == {1: [], 2: [1]}


def test_overlap_dependencies_job_without_investigation_is_free() -> None:
    """A job with no `files_to_modify` is its own singleton component."""
    from sdlc.queue import overlap_dependencies

    rows = [(1, "/repo"), (2, "/repo")]
    assert overlap_dependencies(rows, {1: set(), 2: set()}) == {1: [], 2: []}


def test_overlap_holds_reports_the_pending_predecessor(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.add_job(repo="/repo", kind="fix", scope="1")
    second = store.add_job(repo="/repo", kind="fix", scope="2")
    store.record_files(first, ["src/a.py"])
    store.record_files(second, ["src/a.py", "src/b.py"])

    assert store.overlap_holds() == {second: first}


def test_peek_claimable_holds_back_the_overlapping_job(tmp_path) -> None:
    """AC2: the second overlapping job waits; a disjoint one does not."""
    store = _store(tmp_path)
    first = store.add_job(repo="/repo", kind="fix", scope="1")
    second = store.add_job(repo="/repo", kind="fix", scope="2")
    disjoint = store.add_job(repo="/repo", kind="fix", scope="3")
    store.record_files(first, ["src/a.py"])
    store.record_files(second, ["src/a.py"])
    store.record_files(disjoint, ["src/z.py"])

    assert [j.id for j in store.peek_claimable(now=_now())] == [first, disjoint]


def test_overlap_hold_lifts_once_the_predecessor_is_terminal(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.add_job(repo="/repo", kind="fix", scope="1")
    second = store.add_job(repo="/repo", kind="fix", scope="2")
    store.record_files(first, ["src/a.py"])
    store.record_files(second, ["src/a.py"])

    store.finish_job(first, "done")
    assert store.overlap_holds() == {}
    assert [j.id for j in store.peek_claimable(now=_now())] == [second]


def test_record_files_round_trips_as_json(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/repo", kind="fix", scope="1")
    store.record_files(job_id, ["src/a.py", "src/b.py"])
    assert json.loads(store.get_job(job_id).files) == ["src/a.py", "src/b.py"]


def test_overlap_holds_empty_when_the_queue_was_never_created(tmp_path) -> None:
    """A read verb must not conjure a queue.db — no rows, no holds, no crash."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    assert store.overlap_holds() == {}


def test_files_to_modify_ignores_junk_json() -> None:
    """A corrupted `files` column reads as no investigated files, not a crash."""
    from sdlc.queue import JobRecord

    record = JobRecord(
        id=1, repo="/a", kind="fix", scope="1", priority="normal", state="queued",
        claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at="", updated_at="", reason=None, budget=None, files="not json",
    )
    assert record.files_to_modify() == set()


def test_files_to_modify_ignores_non_list_json() -> None:
    """Valid JSON that is not an array is still not a file list."""
    from sdlc.queue import JobRecord

    record = JobRecord(
        id=1, repo="/a", kind="fix", scope="1", priority="normal", state="queued",
        claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at="", updated_at="", reason=None, budget=None,
        files=json.dumps({"src/a.py": True}),
    )
    assert record.files_to_modify() == set()


# --- AC3/AC4: per-class budgets --------------------------------------------


def test_budget_for_is_defined_for_every_priority_class() -> None:
    from sdlc.queue import PRIORITY_CLASSES, budget_for

    for name in PRIORITY_CLASSES:
        budget = budget_for(name)
        assert budget.max_fix_rounds > 0
        assert budget.wall_clock_seconds > 0


def test_budget_gives_a_higher_class_more_rope() -> None:
    from sdlc.queue import budget_for

    assert budget_for("urgent").max_fix_rounds >= budget_for("low").max_fix_rounds
    assert (
        budget_for("urgent").wall_clock_seconds >= budget_for("low").wall_clock_seconds
    )


def test_budget_for_honours_env_overrides(monkeypatch) -> None:
    """AC4: the budgets are config, not constants."""
    from sdlc.queue import budget_for

    monkeypatch.setenv("SDLC_QUEUE_MAX_FIX_ROUNDS", "2")
    monkeypatch.setenv("SDLC_QUEUE_WALL_CLOCK_MINUTES", "15")
    budget = budget_for("normal")
    assert budget.max_fix_rounds == 2
    assert budget.wall_clock_seconds == 900


@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_budget_for_ignores_a_junk_override(monkeypatch, value) -> None:
    """A malformed override degrades to the default rather than disarming the cap."""
    from sdlc.queue import DEFAULT_BUDGETS, budget_for

    monkeypatch.setenv("SDLC_QUEUE_MAX_FIX_ROUNDS", value)
    monkeypatch.setenv("SDLC_QUEUE_WALL_CLOCK_MINUTES", value)
    assert budget_for("normal") == DEFAULT_BUDGETS["normal"]


def test_budget_breach_returns_none_inside_the_budget() -> None:
    from sdlc.queue import JobBudget, budget_breach

    budget = JobBudget(max_fix_rounds=5, wall_clock_seconds=3600)
    assert budget_breach(budget, elapsed_seconds=10, fix_rounds=1) is None


def test_budget_breach_names_the_fix_round_cap() -> None:
    from sdlc.queue import JobBudget, budget_breach

    budget = JobBudget(max_fix_rounds=5, wall_clock_seconds=3600)
    reason = budget_breach(budget, elapsed_seconds=10, fix_rounds=5)
    assert reason is not None and "fix round" in reason


def test_budget_breach_names_the_wall_clock_cap() -> None:
    from sdlc.queue import JobBudget, budget_breach

    budget = JobBudget(max_fix_rounds=5, wall_clock_seconds=60)
    reason = budget_breach(budget, elapsed_seconds=61, fix_rounds=0)
    assert reason is not None and "wall-clock" in reason


def test_add_job_records_the_class_budget(tmp_path) -> None:
    """AC4: the budget is recorded on the job, not recomputed at read time."""
    from sdlc.queue import budget_for

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3", priority="low")
    job = store.get_job(job_id)
    assert json.loads(job.budget) == budget_for("low").to_dict()
    assert job.job_budget() == budget_for("low")


def test_prioritise_restamps_the_budget_for_the_new_class(tmp_path) -> None:
    from sdlc.queue import budget_for

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3", priority="low")
    store.prioritise_job(job_id, "urgent")
    assert store.get_job(job_id).job_budget() == budget_for("urgent")


def test_job_budget_falls_back_when_the_column_is_junk(tmp_path) -> None:
    """A row written before this story (or corrupted) still yields a live cap."""
    from sdlc.queue import JobRecord, budget_for

    record = JobRecord(
        id=1, repo="/a", kind="fix", scope="1", priority="normal", state="queued",
        claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at="", updated_at="", reason=None, budget="not json", files=None,
    )
    assert record.job_budget() == budget_for("normal")


def test_job_budget_falls_back_when_the_column_is_absent(tmp_path) -> None:
    """A row with no `budget` at all (pre-migration, or never re-stamped) still
    yields a live cap — the class default, not `None`."""
    from sdlc.queue import JobRecord, budget_for

    record = JobRecord(
        id=1, repo="/a", kind="fix", scope="1", priority="normal", state="queued",
        claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at="", updated_at="", reason=None, budget=None, files=None,
    )
    assert record.job_budget() == budget_for("normal")


def test_needs_attention_is_a_terminal_park(tmp_path) -> None:
    """AC3's park is terminal, cancellable and requeueable — never a dead end."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="1")
    store.finish_job(job_id, "needs_attention", reason="budget exhausted")

    job = store.get_job(job_id)
    assert job.state == "needs_attention"
    assert job.reason == "budget exhausted"

    store.requeue_job(job_id)
    assert store.get_job(job_id).state == "queued"
