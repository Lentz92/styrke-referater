"""A paid Claude call, end to end: asking Claude Code headless (`claude -p`), the model pin, the run's cost and time
limits, calls in parallel, what they used, the provenance a result records, and how a step's usage is written to a
run log.

Every Claude call of the pipeline, the audit and the evaluation goes through `ask`, so it runs on the logged-in
subscription and needs no API key. What a caller asks, where it keeps the answer and which run log it writes stay
with the caller: styrke/analyze.py, styrke/incremental.py, styrke/audit.py and styrke/evaluate.py.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

# For the call estimate printed before a command starts: Danish text runs about this many characters per token.
CHARS_PER_TOKEN = 3.2

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    """What Claude calls used, as the CLI reports it; adds up over the attempts of a call and the calls of a step.

    cost_usd is the CLI's estimate at API list price (`total_cost_usd`). The subscription is not billed
    per call, but the estimate measures how much of it a run takes, and RunBudget limits it.
    """
    model: str = ""  # as requested: an alias such as "sonnet", or a full id
    output_by_model: dict[str, int] = field(default_factory=dict)  # canonical id -> output tokens
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0  # time spent waiting on the CLI, retries included, back-off pauses not
    attempts: int = 0

    @classmethod
    def of(cls, result: dict, model: str, duration_s: float) -> Usage:
        """One attempt's usage from the CLI's JSON result, whose `modelUsage` has an entry per model used.
        Entries it cannot read count as nothing rather than losing the attempt's cost."""
        per_model = result.get("modelUsage")
        per_model = per_model if isinstance(per_model, dict) else {}
        entries = [(name, entry) for name, entry in per_model.items() if isinstance(entry, dict)]
        output_by_model: dict[str, int] = {}
        for name, entry in entries:
            canonical = str(entry.get("canonicalModel") or name)
            output_by_model[canonical] = output_by_model.get(canonical, 0) + _count(entry.get("outputTokens"))
        return cls(
            model=model,
            output_by_model=output_by_model,
            input_tokens=sum(_count(entry.get("inputTokens")) for _, entry in entries),
            cache_read_tokens=sum(_count(entry.get("cacheReadInputTokens")) for _, entry in entries),
            cache_write_tokens=sum(_count(entry.get("cacheCreationInputTokens")) for _, entry in entries),
            cost_usd=_cost(result),
            duration_s=duration_s,
            attempts=1,
        )

    def __add__(self, other: Usage) -> Usage:
        output_by_model = dict(self.output_by_model)
        for name, tokens in other.output_by_model.items():
            output_by_model[name] = output_by_model.get(name, 0) + tokens
        counts = {f.name: getattr(self, f.name) + getattr(other, f.name)
                  for f in fields(self) if f.name not in ("model", "output_by_model")}
        return Usage(model=self.model or other.model, output_by_model=output_by_model, **counts)

    @property
    def models(self) -> tuple[str, ...]:
        """Canonical ids the CLI reports having used, sorted."""
        return tuple(sorted(self.output_by_model))

    @property
    def output_tokens(self) -> int:
        return sum(self.output_by_model.values())

    @property
    def all_input_tokens(self) -> int:
        """Input including the cached prompt: with caching, input_tokens alone is only the uncached tail."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def answered_by(self) -> str:
        """The canonical model behind the answer: the requested id when the CLI reports it, else the model that
        wrote the most (an alias call may also use a small helper model)."""
        if self.model in self.output_by_model or not self.output_by_model:
            return self.model
        return max(self.models, key=self.output_by_model.__getitem__)


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _cost(result: dict) -> float:
    value = result.get("total_cost_usd")
    return float(value) if isinstance(value, (int, float)) else 0.0


class RunBudget:
    """The run's limits: no Claude call or retry starts once the time is up, the list-price cost is reached,
    or the run was stopped (a wrong CLI makes every call fail).

    Calls already running finish, so the cost can end up to one call per worker over the limit.
    Thread-safe: worker threads add the cost of each attempt as it completes.
    """

    def __init__(self, *, minutes: float | None = None, max_cost_usd: float | None = None) -> None:
        self._deadline = None if minutes is None else time.monotonic() + 60 * minutes
        self._max_cost_usd = max_cost_usd
        self._spent_usd = 0.0
        self._stopped: str | None = None
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent_usd

    def add(self, cost_usd: float) -> None:
        with self._lock:
            self._spent_usd += cost_usd

    def stop(self, reason: str) -> None:
        """Start no more work in this run; the first reason given is the one reported."""
        with self._lock:
            self._stopped = self._stopped or reason

    def exhausted(self) -> str | None:
        """Why no new work may start, worded for the log, or None while there is room."""
        with self._lock:
            stopped, spent = self._stopped, self._spent_usd
        if stopped:
            return stopped
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return "the time budget is spent"
        if self._max_cost_usd is not None and spent >= self._max_cost_usd:
            return f"the cost limit of {self._max_cost_usd:.2f} USD is reached"
        return None


class ClaudeError(RuntimeError):
    """A Claude job failed, in the call or in handling its answer; `usage` is what it used all the same."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or Usage()


class ModelMismatch(ClaudeError):
    """The CLI answered with another model than the full id asked for: an older CLI may map an id it
    does not know to another model. Not retried, and it stops the run: every other call would get the same."""


class BudgetExhausted(RuntimeError):
    """A run limit was reached before the job started; the job is left for the next run."""


@contextmanager
def usage_kept(usage: Usage) -> Iterator[None]:
    """Errors after a successful call (a quote that breaks the locator, a full disk) still carry the call's
    usage, so its cost reaches the step summary and the run log."""
    try:
        yield
    except Exception as exc:
        raise ClaudeError(f"handling the answer failed: {type(exc).__name__}: {exc}", usage) from exc


def provenance(usage: Usage, cli: str, system: str, schema: dict, effort: str | None) -> dict:
    """What produced a cached result, to tell results apart when the model, CLI, prompt or effort changes.

    Only the model is part of a cache key, the extraction's: switching from an alias to the id it
    stands for must not re-run anything. Claude Code does not report its default effort, so an unset one is recorded
    as "default".
    """
    return {"model": usage.answered_by, "cli": cli, "prompt": prompt_hash(system, schema),
            "effort": effort or "default"}


def prompt_hash(system: str, schema: dict) -> str:
    """Short fingerprint of what Claude is told: the system prompt and the output schema."""
    text = system + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@functools.cache
def cli_version() -> str:
    """The Claude Code version, e.g. "2.1.294", read once per run. It matters: the CLI decides which model
    an alias stands for and what list price it reports. It never stops a run: results are just as valid
    without it, so a version that cannot be read is recorded as "unknown"."""
    claude = shutil.which("claude")
    if claude is None:
        log.warning("Cannot read the Claude Code version: 'claude' is not on PATH")
        return "unknown"
    try:
        proc = subprocess.run([claude, "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=True, timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("Cannot read the Claude Code version: %s", exc)
        return "unknown"
    match = re.match(r"\s*(\d+(?:\.\d+)+)", proc.stdout)
    if not match:
        log.warning("Cannot read the Claude Code version from %r", proc.stdout[:80])
        return "unknown"
    return match[1]


def _claude_path() -> str:
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("Claude Code CLI ('claude') was not found on PATH.")
    return claude


def ask(system: str, prompt: str, schema: dict, *, model: str, effort: str | None, timeout: float,
        budget: RunBudget | None = None, attempts: int = 3) -> tuple[dict, Usage]:
    """Run one headless Claude Code call with structured output; returns (output, usage of all attempts).

    Each attempt may take `timeout` seconds and adds its cost to `budget` before anything else is read from
    its answer; no retry starts once the budget is exhausted. A call that failed and was not retried for
    that reason is still a failure: ClaudeError with the real error, not BudgetExhausted, which only means
    the job never started. A full model id ("claude-…") must be among the models the CLI reports using,
    else ModelMismatch.
    """
    cmd = [
        _claude_path(), "-p", "--output-format", "json", "--no-session-persistence",
        # No tools, plugins, hooks, CLAUDE.md or MCP servers: just the prompt and the schema.
        "--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
        "--model", model, "--system-prompt", system, "--json-schema", json.dumps(schema),
    ]
    if effort:
        cmd += ["--effort", effort]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")}

    usage = Usage(model=model)
    error = ""
    for attempt in range(1, attempts + 1):
        limit = budget.exhausted() if budget is not None and attempt > 1 else None
        if limit:
            raise ClaudeError(f"{error} (not retried after {attempt - 1} attempts because {limit})", usage)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=False,
                                      timeout=timeout, cwd=cwd, env=env)
            except subprocess.TimeoutExpired:
                usage += Usage(model=model, duration_s=time.monotonic() - started, attempts=1)
                error = "timeout"
                continue
        result = _json_object(proc.stdout)
        if budget is not None:
            budget.add(_cost(result))
        spent = Usage.of(result, model, time.monotonic() - started)
        usage += spent
        output = result.get("structured_output")
        if proc.returncode == 0 and not result.get("is_error") and isinstance(output, dict):
            if model.startswith("claude-") and model not in spent.models:
                raise ModelMismatch(f"asked for {model}, but Claude Code answered with "
                                    f"{', '.join(spent.models) or 'an unknown model'}; check its version", usage)
            return output, usage
        error = str(result.get("result") or proc.stderr or proc.stdout)[-500:]
        if attempt < attempts:
            time.sleep(15 * attempt)
    raise ClaudeError(error, usage)


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- calls in parallel

@dataclass(frozen=True)
class StepSummary:
    """One step's Claude calls, for the log, a run log (step_json) and the GitHub step summary."""
    label: str
    calls: int  # jobs that started, whether they succeeded or failed
    failed: int
    skipped: int  # jobs that never started because a limit was reached; they run next time
    usage: Usage  # summed over every attempt, failed ones included
    seconds: float  # wall-clock time of the step
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)  # heading -> lines to review, for run-report.md


def run_parallel(jobs: list, fn, workers: int, label: str, budget: RunBudget | None = None) -> StepSummary:
    """Run fn over jobs in a thread pool; log progress and failures, keep going on errors.

    fn returns (message, Usage). Jobs that have not started when the budget is exhausted are skipped.
    The first ModelMismatch stops the budget, so the rest of the run is skipped too.
    """
    budget = budget if budget is not None else RunBudget()

    def start(job):
        if limit := budget.exhausted():
            raise BudgetExhausted(limit)
        try:
            return fn(job)
        except ModelMismatch:  # stop here, before this worker takes the next job
            budget.stop("Claude Code answered with another model than requested (check its version)")
            raise

    started = time.monotonic()
    usage = Usage()
    failed = skipped = 0
    limits: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(start, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                message, job_usage = future.result()
            except BudgetExhausted as exc:
                skipped += 1
                limits.add(str(exc))
                continue
            except Exception as exc:  # one failed call must not stop the batch; rerun picks it up
                failed += 1
                if isinstance(exc, ClaudeError):
                    usage += exc.usage
                log.error("%s [%d/%d] failed: %s", label, done, len(jobs), exc)
                continue
            usage += job_usage
            log.info("%s [%d/%d] %s", label, done, len(jobs), message)
    if failed:
        log.warning("%s: %d failed; run again to retry them", label, failed)
    if skipped:
        log.warning("%s: skipped %d because %s; they run next time", label, skipped, " and ".join(sorted(limits)))
    log.info("%s done: %d calls, %d tokens in and %d out (%.2f USD at API list price, paid by the subscription)",
             label, len(jobs) - skipped, usage.all_input_tokens, usage.output_tokens, usage.cost_usd)
    return StepSummary(label, len(jobs) - skipped, failed, skipped, usage, time.monotonic() - started)


# --------------------------------------------------------------------------- usage records

def usage_json(usage: Usage) -> dict:
    """One call's usage as a kept answer records it (styrke/audit.py's data/audit/, styrke/evaluate.py's
    eval/runs/<run>/)."""
    return {"input": usage.input_tokens, "cache_read": usage.cache_read_tokens,
            "cache_write": usage.cache_write_tokens, "output": usage.output_tokens,
            "cost_usd": round(usage.cost_usd, 4), "seconds": round(usage.duration_s), "attempts": usage.attempts,
            "models": list(usage.models)}


def describe_usage(usage: Usage) -> str:
    """One call's usage, for the line that logs it."""
    return (f"{usage.all_input_tokens:,} tokens in, {usage.output_tokens:,} out, {usage.cost_usd:.2f} USD, "
            f"{usage.duration_s:.0f} s")


def estimate_tokens(system: str, prompt: str, schema: dict) -> int:
    """A call's input tokens, roughly, for the estimate printed before it is made."""
    return round((len(system) + len(prompt) + len(json.dumps(schema))) / CHARS_PER_TOKEN)


def _step_json(step: StepSummary) -> dict:
    """One step's Claude usage as a line of a run log stores it (data/runs.jsonl, eval/runs.jsonl)."""
    usage = step.usage
    return {
        "calls": step.calls,
        "failed": step.failed,
        "skipped": step.skipped,
        "tokens": {"input": usage.input_tokens, "output": usage.output_tokens,
                   "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens},
        "cost_usd": round(usage.cost_usd, 4),
        "models": list(usage.models),
        "seconds": round(step.seconds),
    }


def append_run_log(path: Path, now: datetime, header: Mapping[str, object],
                   steps: Mapping[str, StepSummary]) -> None:
    """Add one JSON line to a run log, so cost and time can be followed over months: the time, the caller's `header`
    fields in the order given (each log has its own), then what each step's Claude calls used. After a line cut off by
    a run killed while appending, the new line starts on a line of its own."""
    line = {"time": now.isoformat(timespec="seconds"), **header,
            "steps": {name: _step_json(step) for name, step in steps.items()}}
    text = json.dumps(line, ensure_ascii=False) + "\n"
    if _ends_mid_line(path):
        text = "\n" + text
    with path.open("a") as out:
        out.write(text)


def _ends_mid_line(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as log_file:
        log_file.seek(-1, os.SEEK_END)
        return log_file.read(1) != b"\n"


def read_run_log(path: Path) -> list[dict]:
    """The lines of a run log; none when it does not exist yet. A line cut off by a run killed while appending it is
    skipped with a warning: it loses that run's record, not every later report that reads the log."""
    if not path.exists():
        return []
    lines = []
    for number, text in enumerate(path.read_text().splitlines(), 1):
        if not text.strip():
            continue
        try:
            lines.append(json.loads(text))
        except json.JSONDecodeError:
            log.warning("%s line %d is not a whole JSON line (cut off by a killed run?); skipped", path, number)
    return lines
