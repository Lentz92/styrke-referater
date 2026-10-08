import subprocess
import time

import pytest

import analyze
from analyze import ClaudeError, _run_parallel, ask_claude


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


def test_failed_call_not_retried_after_the_deadline_reports_its_error(monkeypatch, caplog):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="API Error: overloaded")

    monkeypatch.setattr(analyze.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(analyze.subprocess, "run", run)
    monkeypatch.setattr(analyze.time, "sleep", lambda _: None)
    deadline = time.monotonic() - 1  # passed while the first attempt ran; that attempt still counts

    def job(_):
        return ask_claude("system", "prompt", {}, model="sonnet", effort=None, timeout=1, deadline=deadline)

    with pytest.raises(ClaudeError, match="API Error: overloaded.*tidsbudgettet"):
        job(None)
    assert len(calls) == 1

    _, failed = _run_parallel([1], job, workers=1, label="Test")
    assert failed == 1 and "fejlede: API Error: overloaded" in caplog.text
