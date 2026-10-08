import time

from analyze import _run_parallel


def test_jobs_after_the_deadline_are_skipped_and_counted():
    ran = []

    def job(n):
        ran.append(n)
        return f"job {n}", 0.5

    cost, failed = _run_parallel([1, 2, 3], job, workers=2, label="Test", deadline=time.monotonic() - 1)
    assert (ran, cost, failed) == ([], 0.0, 3)


def test_failures_are_counted_and_the_rest_still_runs():
    def job(n):
        if n == 2:
            raise RuntimeError("boom")
        return f"job {n}", 0.5

    cost, failed = _run_parallel([1, 2, 3], job, workers=2, label="Test")
    assert (cost, failed) == (1.0, 1)
