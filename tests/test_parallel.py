import pytest

from analyze import ClaudeError, RunBudget, Usage, _run_parallel


def test_jobs_after_the_deadline_are_skipped_and_counted():
    ran = []

    def job(n):
        ran.append(n)
        return f"job {n}", Usage(cost_usd=0.5)

    step = _run_parallel([1, 2, 3], job, workers=2, label="Test", budget=RunBudget(minutes=-1))
    assert (ran, step.calls, step.skipped, step.usage.cost_usd) == ([], 0, 3, 0.0)


def test_jobs_after_the_cost_limit_are_skipped_and_the_limit_is_named(caplog):
    budget = RunBudget(max_cost_usd=1.0)

    def job(n):
        budget.add(0.5)  # ask_claude adds the cost of each attempt as it completes
        return f"job {n}", Usage(cost_usd=0.5)

    step = _run_parallel([1, 2, 3, 4], job, workers=1, label="Test", budget=budget)
    assert (step.calls, step.skipped, step.usage.cost_usd) == (2, 2, 1.0)
    assert "skipped 2 because the cost limit of 1.00 USD is reached" in caplog.text


def test_failures_are_counted_with_their_usage_and_the_rest_still_runs():
    def job(n):
        if n == 2:
            raise ClaudeError("boom", Usage(cost_usd=0.25, attempts=3))
        return f"job {n}", Usage(model="sonnet", output_by_model={"claude-sonnet-5-5": 10}, cost_usd=0.5, attempts=1)

    step = _run_parallel([1, 2, 3], job, workers=2, label="Test")
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
