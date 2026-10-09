import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze  # noqa: E402
from analyze import Decision  # noqa: E402

_BASE = Decision(
    ref="doc#1", doc_id="doc", dato="2020-03-01", emne="Licensgebyr", kategori="okonomi", udfald="vedtaget",
    handling="ny", niveau="staevneregel", tekst="Licensgebyret er 200 kr.", citat="licensgebyret er 200 kr",
    citat_fundet=True, side=1, side_rettet=False, rank=0, stemmer=None, forslagsstiller=None,
    gaelder_fra=None, gaelder_til=None,
)


def decision(**changes) -> Decision:
    return replace(_BASE, **changes)


@pytest.fixture(autouse=True)
def _slug_registry(tmp_path_factory, monkeypatch):
    """Every test gets its own data/slugs.json, so none can write the real one; outside the test's tmp_path,
    which some tests use as data/regler/ itself."""
    monkeypatch.setattr(analyze, "SLUGS_PATH", tmp_path_factory.mktemp("registry") / "slugs.json")


# A fake `claude` that prints what Claude Code 2.1.294 prints. plan.json lists what each call does, in order
# (the last step repeats): "ok"; "error" (exit 1, costs 0.10); "other" (answers with Haiku); "sleep";
# "helper-before"/"helper-after" (a helper model that writes less, sorting before/after the main one);
# "keyed" (modelUsage keyed by another name than canonicalModel, plus an entry that is not an object).
# output.json, if present, is the structured output; outputs.json, if present, lists one per call (the last
# repeats). Each call's arguments are appended to argv.jsonl, and its prompt (stdin) to prompts.jsonl.
FAKE_CLAUDE = """\
import json, sys, time
from pathlib import Path

here = Path(__file__).parent
with (here / "invocations").open("a") as log:
    log.write(("version" if sys.argv[1:] == ["--version"] else "call") + "\\n")
if sys.argv[1:] != ["--version"]:
    with (here / "argv.jsonl").open("a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    with (here / "prompts.jsonl").open("a") as log:
        log.write(json.dumps(sys.stdin.read()) + "\\n")
if sys.argv[1:] == ["--version"]:
    if (here / "version_fails").exists():
        sys.exit(1)
    if (here / "version_garbled").exists():
        sys.stdout.buffer.write(b"2.1.294 \\xff\\xfe (Claude Code)\\n")
        sys.exit(0)
    print("2.1.294 (Claude Code)")
    sys.exit(0)
calls = (here / "invocations").read_text().split().count("call")
plan = json.loads((here / "plan.json").read_text()) if (here / "plan.json").exists() else ["ok"]
step = plan[min(calls, len(plan)) - 1]
if step == "sleep":
    time.sleep(10)
requested = sys.argv[sys.argv.index("--model") + 1]
model = "claude-haiku-5-5" if step == "other" else {"sonnet": "claude-sonnet-5-5"}.get(requested, requested)
cost = 0.1 if step == "error" else 0.25
entry = {"inputTokens": 2, "outputTokens": 52, "cacheReadInputTokens": 967, "cacheCreationInputTokens": 2514,
         "costUSD": cost, "canonicalModel": model, "costBasis": "list"}
usage = {model: entry}
if step.startswith("helper"):
    helper = "claude-aaa-helper" if step == "helper-before" else "claude-zzz-helper"
    usage[helper] = {**entry, "outputTokens": 5, "canonicalModel": helper}
if step == "keyed":
    usage = {"us.anthropic." + model: entry, "junk": "not an object"}
result = {"type": "result", "is_error": step == "error", "total_cost_usd": cost, "duration_ms": 1200,
          "num_turns": 1, "modelUsage": usage}
if step == "error":
    print(json.dumps({**result, "result": "API Error: overloaded"}))
    sys.exit(1)
output = here / "output.json"
answer = json.loads(output.read_text()) if output.exists() else {"answer": "ok"}
if (here / "outputs.json").exists():
    outputs = json.loads((here / "outputs.json").read_text())
    answer = outputs[min(calls, len(outputs)) - 1]
print(json.dumps({**result, "result": "", "structured_output": answer}))
"""


class FakeClaude:
    def __init__(self, bin_dir: Path):
        self.bin_dir = bin_dir

    def plan(self, *steps: str) -> None:
        (self.bin_dir / "plan.json").write_text(json.dumps(steps))

    def answer(self, output: dict) -> None:
        (self.bin_dir / "output.json").write_text(json.dumps(output))

    def answers(self, *outputs: dict) -> None:
        """One structured output per call, in order; the last repeats."""
        (self.bin_dir / "outputs.json").write_text(json.dumps(outputs))

    def calls(self) -> list[list[str]]:
        """The arguments of every Claude call so far, in order."""
        log = self.bin_dir / "argv.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def prompts(self) -> list[str]:
        """The prompt (stdin) of every Claude call so far, in order."""
        log = self.bin_dir / "prompts.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def fail_version(self) -> None:
        (self.bin_dir / "version_fails").touch()

    def garble_version(self) -> None:
        """`claude --version` prints bytes that are not UTF-8."""
        (self.bin_dir / "version_garbled").touch()

    def invocations(self, kind: str) -> int:
        """How often the CLI ran: "call" for a Claude call, "version" for `claude --version`."""
        log = self.bin_dir / "invocations"
        return log.read_text().split().count(kind) if log.exists() else 0


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(analyze.time, "sleep", lambda _: None)  # the back-off between attempts
    analyze.cli_version.cache_clear()
    yield FakeClaude(bin_dir)
    analyze.cli_version.cache_clear()
