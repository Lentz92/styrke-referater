"""styrke/claude.py: Claude calls against the fake `claude` from conftest.py, calls in parallel under the run budget,
and the run log; no real Claude calls."""

import json
from datetime import datetime, timezone

import pytest

from styrke import claude
from styrke.claude import ClaudeError, ModelMismatch, RunBudget, StepSummary, Usage, run_parallel


def _ask(model="claude-sonnet-5-5", **kwargs):
    return claude.ask("system", "prompt", {"type": "object"}, model=model, effort=None, timeout=30, **kwargs)


# ---------------------------------------------------------------- usage and models

def test_usage_is_read_from_the_cli_result(fake_claude):
    fake_claude.plan("ok")
    output, usage = _ask()
    assert output == {"answer": "ok"}
    assert usage.models == ("claude-sonnet-5-5",) and usage.answered_by == "claude-sonnet-5-5"
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == \
        (2, 52, 967, 2514)
    assert (usage.cost_usd, usage.attempts) == (0.25, 1)


def test_an_alias_is_allowed_and_its_canonical_model_recorded(fake_claude):
    fake_claude.plan("ok")
    _, usage = _ask(model="sonnet")
    assert (usage.model, usage.answered_by) == ("sonnet", "claude-sonnet-5-5")


@pytest.mark.parametrize("step", ["helper-before", "helper-after"])
def test_with_a_helper_model_the_answer_comes_from_the_one_that_wrote_most(fake_claude, step):
    fake_claude.plan(step)
    _, usage = _ask(model="sonnet")
    assert len(usage.models) == 2
    assert usage.answered_by == "claude-sonnet-5-5"


def test_the_canonical_model_counts_not_the_usage_key_and_odd_entries_are_skipped(fake_claude):
    fake_claude.plan("keyed")
    _, usage = _ask()
    assert usage.models == ("claude-sonnet-5-5",)
    assert (usage.output_tokens, usage.cost_usd) == (52, 0.25)


def test_another_model_than_the_full_id_fails_without_retry_and_stops_the_run(fake_claude, caplog):
    fake_claude.plan("other")
    with pytest.raises(ModelMismatch, match="claude-sonnet-5-5.*claude-haiku-5-5"):
        _ask()
    assert fake_claude.invocations("call") == 1

    budget = RunBudget()
    first = run_parallel([1, 2, 3], lambda _: _ask(), workers=1, label="Extract", budget=budget)
    second = run_parallel([1, 2], lambda _: _ask(), workers=1, label="Consolidate", budget=budget)
    assert (first.failed, first.skipped, first.usage.cost_usd) == (1, 2, 0.25)
    assert (second.calls, second.skipped) == (0, 2)
    assert fake_claude.invocations("call") == 2
    assert "another model than requested" in caplog.text


# ---------------------------------------------------------------- retries, timeouts and limits

def test_a_failed_attempt_is_retried_and_its_cost_counted(fake_claude):
    fake_claude.plan("error", "ok")
    budget = RunBudget()
    output, usage = _ask(budget=budget)
    assert output == {"answer": "ok"}
    assert usage.attempts == 2 and fake_claude.invocations("call") == 2
    assert usage.cost_usd == pytest.approx(0.35) and budget.spent_usd == pytest.approx(0.35)


def test_a_call_that_times_out_fails(fake_claude):
    fake_claude.plan("sleep")
    with pytest.raises(ClaudeError, match="timeout"):
        claude.ask("system", "prompt", {}, model="claude-sonnet-5-5", effort=None, timeout=0.5, attempts=1)


def test_no_retry_once_the_cost_limit_is_reached(fake_claude):
    fake_claude.plan("error", "ok")
    with pytest.raises(ClaudeError, match="API Error: overloaded.*cost limit"):
        _ask(budget=RunBudget(max_cost_usd=0.1))
    assert fake_claude.invocations("call") == 1


def test_failed_call_not_retried_after_the_deadline_reports_its_error(fake_claude, caplog):
    fake_claude.plan("error")
    budget = RunBudget(minutes=-1)  # passed while the first attempt ran; that attempt still counts

    with pytest.raises(ClaudeError, match="API Error: overloaded.*time budget"):
        _ask(budget=budget)
    assert fake_claude.invocations("call") == 1

    step = run_parallel([1], lambda _: _ask(budget=budget), workers=1, label="Test")
    assert step.failed == 1 and "failed: API Error: overloaded" in caplog.text


# ---------------------------------------------------------------- CLI version

def test_an_unreadable_cli_version_does_not_stop_the_run(fake_claude, caplog):
    fake_claude.fail_version()
    assert claude.cli_version() == "unknown"
    assert "Cannot read the Claude Code version" in caplog.text


def test_a_cli_version_that_is_not_utf8_is_still_read(fake_claude):
    fake_claude.garble_version()
    assert claude.cli_version() == "2.1.294"


def test_no_cli_on_path_gives_an_unknown_version(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    claude.cli_version.cache_clear()
    try:
        assert claude.cli_version() == "unknown"
    finally:
        claude.cli_version.cache_clear()


# ---------------------------------------------------------------- run_parallel and the run budget

def test_jobs_after_the_deadline_are_skipped_and_counted():
    ran = []

    def job(n):
        ran.append(n)
        return f"job {n}", Usage(cost_usd=0.5)

    step = run_parallel([1, 2, 3], job, workers=2, label="Test", budget=RunBudget(minutes=-1))
    assert (ran, step.calls, step.skipped, step.usage.cost_usd) == ([], 0, 3, 0.0)


def test_jobs_after_the_cost_limit_are_skipped_and_the_limit_is_named(caplog):
    budget = RunBudget(max_cost_usd=1.0)

    def job(n):
        budget.add(0.5)  # claude.ask adds the cost of each attempt as it completes
        return f"job {n}", Usage(cost_usd=0.5)

    step = run_parallel([1, 2, 3, 4], job, workers=1, label="Test", budget=budget)
    assert (step.calls, step.skipped, step.usage.cost_usd) == (2, 2, 1.0)
    assert "skipped 2 because the cost limit of 1.00 USD is reached" in caplog.text


def test_failures_are_counted_with_their_usage_and_the_rest_still_runs():
    def job(n):
        if n == 2:
            raise ClaudeError("boom", Usage(cost_usd=0.25, attempts=3))
        return f"job {n}", Usage(model="sonnet", output_by_model={"claude-sonnet-5-5": 10}, cost_usd=0.5, attempts=1)

    step = run_parallel([1, 2, 3], job, workers=2, label="Test")
    assert (step.calls, step.failed, step.skipped) == (3, 1, 0)
    assert (step.usage.cost_usd, step.usage.attempts, step.usage.output_tokens) == (1.25, 5, 20)
    assert step.usage.models == ("claude-sonnet-5-5",)


def test_budget_without_limits_is_never_exhausted():
    budget = RunBudget()
    budget.add(1000)
    assert budget.exhausted() is None


def test_budget_names_the_limit_that_was_hit():
    assert RunBudget(minutes=60).exhausted() is None
    assert "time budget" in RunBudget(minutes=-1).exhausted()

    budget = RunBudget(max_cost_usd=1.0)
    budget.add(0.6)
    assert budget.exhausted() is None
    budget.add(0.4)
    assert budget.exhausted() == "the cost limit of 1.00 USD is reached"
    assert budget.spent_usd == pytest.approx(1.0)


def test_a_stopped_budget_reports_the_first_reason():
    budget = RunBudget(max_cost_usd=100)
    budget.stop("wrong model")
    budget.stop("something else")
    assert budget.exhausted() == "wrong model"


# ---------------------------------------------------------------- the run log

def test_each_run_appends_one_line_to_the_run_log(tmp_path):
    def step(label: str, calls: int, cost: float) -> StepSummary:
        usage = Usage(model="claude-sonnet-5-5", output_by_model={"claude-sonnet-5-5": 52}, input_tokens=2,
                      cache_read_tokens=967, cache_write_tokens=2514, cost_usd=cost, attempts=calls)
        return StepSummary(label, calls=calls, failed=1, skipped=2, usage=usage, seconds=61.4)

    path = tmp_path / "runs.jsonl"
    steps = {"extract": step("Extract", 3, 0.123456), "consolidate": step("Consolidate", 0, 0.0)}
    when = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)

    claude.append_run_log(path, when, {"cli": "2.1.294"}, steps)
    claude.append_run_log(path, when, {"cli": "2.1.294"}, steps)

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {
        "time": "2026-11-01T06:00:00+00:00",
        "cli": "2.1.294",
        "steps": {
            "extract": {"calls": 3, "failed": 1, "skipped": 2,
                        "tokens": {"input": 2, "output": 52, "cache_read": 967, "cache_write": 2514},
                        "cost_usd": 0.1235, "models": ["claude-sonnet-5-5"], "seconds": 61},
            "consolidate": {"calls": 0, "failed": 1, "skipped": 2,
                            "tokens": {"input": 2, "output": 52, "cache_read": 967, "cache_write": 2514},
                            "cost_usd": 0.0, "models": ["claude-sonnet-5-5"], "seconds": 61},
        },
    }
    assert path.read_text().startswith('{"time": "2026-11-01T06:00:00+00:00", "cli": "2.1.294", "steps": ')
    assert claude.read_run_log(path) == lines and claude.read_run_log(tmp_path / "none.jsonl") == []
