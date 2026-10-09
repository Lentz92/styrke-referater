"""The workflows in .github/: their steps, and their routing (.github/scripts/route-update.sh and route-audit.sh) run
against a local origin with a fake gh."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import analyze
import audit
import update

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = ROOT / ".github" / "scripts" / "route-update.sh"
AUDIT_SCRIPT = ROOT / ".github" / "scripts" / "route-audit.sh"
CLI_ACTION = "./.github/actions/claude-cli"
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the fake gh applies --jq with jq")

# Keeps pull requests in prs.json and records each call in gh.jsonl. `pr list` filters and projects like gh, then
# applies --jq with jq, so the script's own filter is what runs.
FAKE_GH = """\
import json, subprocess, sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]
state = here / "prs.json"
prs = json.loads(state.read_text()) if state.exists() else []


def option(name):
    return args[args.index(name) + 1] if name in args else None


call = {"args": args}
if option("--body-file"):
    call["body"] = Path(option("--body-file")).read_text()
with (here / "gh.jsonl").open("a") as log:
    log.write(json.dumps(call) + "\\n")
if args[:2] == ["pr", "list"]:
    found = [pr for pr in prs if pr["headRefName"] == option("--head") and pr["state"] == option("--state").upper()]
    out = json.dumps([{field: pr[field] for field in option("--json").split(",")} for pr in found])
    if option("--jq"):
        out = subprocess.run(["jq", "-r", option("--jq")], input=out, capture_output=True, text=True,
                             check=True).stdout
    sys.stdout.write(out)
elif args[:2] == ["pr", "create"]:
    number = max((pr["number"] for pr in prs), default=6) + 1
    prs.append({"number": number, "headRefName": option("--head"), "baseRefName": option("--base"),
                "isCrossRepository": False, "state": "OPEN", "body": call["body"]})
    print(f"https://github.com/owner/repo/pull/{number}")
elif args[:2] == ["pr", "edit"]:
    pr = next(pr for pr in prs if pr["number"] == int(args[2]))
    pr.update(baseRefName=option("--base"), body=call["body"])
elif args[:2] == ["pr", "close"]:
    next(pr for pr in prs if pr["number"] == int(args[2]))["state"] = "CLOSED"
else:
    sys.exit(f"fake gh: unexpected call {args}")
state.write_text(json.dumps(prs))
"""


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.bin = root / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(f"#!{sys.executable}\n{FAKE_GH}")
        fake.chmod(0o755)
        # No user or system git config (commit signing, hooks), and the fake before any real gh.
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(root),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for name in ("GITHUB_RUN_ID", "GH_TOKEN", "GITHUB_TOKEN", "GH_REPO"):
            self.env.pop(name, None)
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.clones = 0
        self.commit_elsewhere("main", ".gitignore", "run-report.md\naudit-report.md\n")
        self.commit_elsewhere("main", "data.json", "{}\n")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.origin, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def clone(self, branch: str) -> Path:
        self.clones += 1
        work = self.root / f"clone{self.clones}"
        self.git("clone", "-q", "-b", branch, str(self.origin), str(work), cwd=self.root)
        return work

    def commit_elsewhere(self, branch: str, name: str, content: str) -> None:
        """A commit someone else pushes to `branch` (created from main if it is new)."""
        self.clones += 1
        work = self.root / f"clone{self.clones}"
        if self.clones == 1:
            self.git("init", "-q", "-b", "main", str(work), cwd=self.root)
        else:
            self.git("clone", "-q", str(self.origin), str(work), cwd=self.root)
            self.git("checkout", "-q", "-B", branch, *([f"origin/{branch}"] if branch in self.branches() else []),
                     cwd=work)
        (work / name).write_text(content)
        self.git("add", "-A", cwd=work)
        self.git("-c", "user.name=Someone", "-c", "user.email=someone@example.com", "commit", "-q", "-m", name,
                 cwd=work)
        self.git("push", "-q", str(self.origin), f"HEAD:refs/heads/{branch}", cwd=work)

    def route(self, code: int, data: str | None = '{"new": 1}', report: str | None = "# Report\n",
              branch: str = "main", race: bool = False) -> subprocess.CompletedProcess:
        """A run like the workflow's: a fresh checkout of the branch, update.py's changes, then the script."""
        work = self.clone(branch)
        if data is not None:
            (work / "data.json").write_text(data + "\n")
        if report is not None:
            (work / "run-report.md").write_text(report)
        if race:
            self.commit_elsewhere(branch, "other.txt", "pushed during the run\n")
        return subprocess.run(["bash", str(UPDATE_SCRIPT), str(code)], cwd=work, env=self.env, capture_output=True,
                              text=True, check=False)

    def gh_calls(self) -> list[dict]:
        log = self.bin / "gh.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def prs(self) -> list[dict]:
        state = self.bin / "prs.json"
        return json.loads(state.read_text()) if state.exists() else []

    def add_pr(self, **pr) -> None:
        (self.bin / "prs.json").write_text(json.dumps([*self.prs(), {"isCrossRepository": False, "state": "OPEN",
                                                                      **pr}]))

    def file(self, ref: str, name: str = "data.json") -> str:
        return self.git("show", f"{ref}:{name}")

    def branches(self) -> set[str]:
        return set(self.git("for-each-ref", "--format=%(refname:short)", "refs/heads").split())


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


# ---------------------------------------------------------------- the monthly update (update.yml, route-update.sh)

PASSED = f"{update.CHECKS_PASSED}\n# Report\n"


@needs_jq
def test_a_run_whose_checks_pass_is_committed_to_main(repo):
    result = repo.route(0)
    assert result.returncode == 0, result.stderr
    assert repo.file("main") == '{"new": 1}'
    assert "Monthly rule overview update" in repo.git("log", "-1", "--format=%s", "main")
    assert repo.branches() == {"main"} and repo.prs() == []


def test_a_run_without_changes_commits_nothing(repo):
    before = repo.git("rev-parse", "main")
    result = repo.route(0, data=None)
    assert result.returncode == 0 and "No changes to commit." in result.stdout
    assert repo.git("rev-parse", "main") == before


@needs_jq
def test_errors_go_to_one_review_branch_and_one_pull_request(repo):
    main = repo.git("rev-parse", "main")

    first = repo.route(3, data='{"first": 1}', report="# First report\n")
    assert first.returncode == 0, first.stderr
    assert repo.git("rev-parse", "main") == main
    assert repo.file("auto/update") == '{"first": 1}' and repo.git("rev-parse", "auto/update~1") == main
    (pr,) = repo.prs()
    assert (pr["headRefName"], pr["baseRefName"], pr["body"]) == ("auto/update", "main", "# First report\n")
    created = repo.gh_calls()[-1]["args"]
    assert created[created.index("--title") + 1] == "Monthly update needs review"

    # The next run starts from main again and replaces the branch; the open pull request gets its report.
    second = repo.route(3, data='{"second": 1}', report="# Second report\n")
    assert second.returncode == 0, second.stderr
    assert repo.git("rev-parse", "main") == main
    assert repo.file("auto/update") == '{"second": 1}' and repo.git("rev-parse", "auto/update~1") == main
    (pr,) = repo.prs()
    assert pr["state"] == "OPEN" and pr["body"] == "# Second report\n"
    assert "run-report.md" not in repo.git("ls-tree", "--name-only", "auto/update")


@needs_jq
@pytest.mark.parametrize(("report", "published"), [(PASSED, True), (f"{update.CHECKS_FAILED}\n", False),
                                                   ("# Report\n", False), (None, False)])
def test_a_failed_run_is_kept_on_main_only_when_its_report_says_the_checks_passed(repo, report, published):
    main = repo.git("rev-parse", "main")
    result = repo.route(1, report=report)
    assert result.returncode == 0, result.stderr
    assert (repo.git("rev-parse", "main") != main) == published
    assert ("auto/update" in repo.branches()) == (not published)


def test_errors_without_changes_are_already_on_main(repo):
    result = repo.route(3, data=None)
    assert result.returncode == 1 and "they are in main already" in result.stderr
    assert repo.branches() == {"main"} and repo.prs() == []
    assert repo.route(1, data=None, report=None).returncode == 0  # e.g. stopped before analysing anything


@needs_jq
def test_a_result_that_cannot_be_pushed_to_main_goes_to_review(repo):
    result = repo.route(0, race=True)
    assert result.returncode == 0, result.stderr
    assert repo.file("main", "other.txt") == "pushed during the run" and repo.file("main") == "{}"
    assert repo.file("auto/update") == '{"new": 1}'
    (pr,) = repo.prs()
    assert pr["body"].startswith("**main changed while this run was going**")


@needs_jq
def test_a_run_on_another_branch_has_its_own_review_branch(repo):
    repo.add_pr(number=3, headRefName="auto/update", baseRefName="main", body="main's review")
    repo.commit_elsewhere("feature", "feature.txt", "x\n")

    result = repo.route(3, branch="feature")
    assert result.returncode == 0, result.stderr
    assert repo.file("auto/update-feature") == '{"new": 1}' and "auto/update" not in repo.branches()
    main_review, feature_review = repo.prs()
    assert (main_review["baseRefName"], main_review["body"]) == ("main", "main's review")
    assert (feature_review["headRefName"], feature_review["baseRefName"]) == ("auto/update-feature", "feature")


@needs_jq
def test_publishing_closes_the_review_left_open_by_an_earlier_run(repo):
    repo.route(3)
    assert "auto/update" in repo.branches()

    result = repo.route(0, data='{"later": 1}')
    assert result.returncode == 0, result.stderr
    (pr,) = repo.prs()
    assert pr["state"] == "CLOSED"
    closed = repo.gh_calls()[-1]["args"]
    assert closed[:3] == ["pr", "close", str(pr["number"])] and "--comment" in closed
    assert repo.branches() == {"main"}


@needs_jq
def test_a_pull_request_from_a_fork_is_not_the_review(repo):
    repo.add_pr(number=3, headRefName="auto/update", baseRefName="main", isCrossRepository=True, body="fork")

    result = repo.route(3)
    assert result.returncode == 0, result.stderr
    fork, review = repo.prs()
    assert fork["body"] == "fork" and review["headRefName"] == "auto/update" and review["number"] == 4


@needs_jq
def test_a_long_report_is_cut_to_fit_a_pull_request(repo):
    result = repo.route(3, report="".join(f"- error {i}\n" for i in range(10_000)))
    assert result.returncode == 0, result.stderr
    body = repo.prs()[-1]["body"]
    assert len(body.encode()) < 65_536 and body.endswith("the run page has all of it.\n")


# ---------------------------------------------------------------- the workflows' steps and the CLI action

def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _job(workflow: dict) -> dict:
    (job,) = workflow["jobs"].values()
    return job


def _step(job: dict, found: str) -> dict:
    """The step named `found`, or with that id, or using that action."""
    return next(step for step in job["steps"] if found in (step.get("name"), step.get("id"), step.get("uses")))


def _in_order(job: dict, *found: str) -> bool:
    positions = [job["steps"].index(_step(job, name)) for name in found]
    return positions == sorted(positions)


def test_both_workflows_can_run_the_cli_action():
    """What GitHub checks only when a workflow runs: a monthly run would fail before it starts."""
    action = yaml.safe_load((ROOT / CLI_ACTION / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert all("shell" in step for step in action["runs"]["steps"] if "run" in step)  # required in a composite
    required = {name for name, spec in action["inputs"].items() if spec.get("required")}
    for name in ("update.yml", "audit.yml"):
        job = _job(_workflow(name))
        passed = set(_step(job, CLI_ACTION).get("with", {}))
        assert required <= passed <= set(action["inputs"]), name
        # A local action is read from the checked-out repository.
        checkout = next(step["uses"] for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert _in_order(job, checkout, CLI_ACTION), name


def _run_step(tmp_path, step: dict, uv_code: int, **env: str) -> list[str]:
    """Run a step's script as GitHub does, with a fake uv that exits with `uv_code` (or $PROPOSE_CODE for `audit.py
    propose`); return the uv commands it ran."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path}/uv.log\n'
                                f'[ "$3" = propose ] && exit "$PROPOSE_CODE"\nexit {uv_code}\n')
    (bin_dir / "uv").chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "GITHUB_OUTPUT": str(tmp_path / "output"), **env}
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], env=env, check=True)
    return (tmp_path / "uv.log").read_text().splitlines()


@pytest.mark.parametrize(("rebuild", "full", "options"), [
    ("", "", ""), ("false", "false", ""), ("true", "false", " --allow-rebuild"),
    ("true", "true", " --allow-rebuild --consolidate-mode full")])
def test_the_monthly_run_passes_the_ticked_boxes_and_no_limits(tmp_path, rebuild, full, options):
    """A scheduled run has no inputs; a manual one passes its boxes, and its exit code is recorded for the routing."""
    step = _step(_job(_workflow("update.yml")), "update")
    assert _run_step(tmp_path, step, 3, REBUILD=rebuild, FULL=full) == [f"run update.py{options}"]
    assert (tmp_path / "output").read_text() == "code=3\n"


def test_the_refusal_names_the_boxes_of_the_monthly_workflow():
    update_yml = _workflow("update.yml")
    inputs = update_yml.get("on", update_yml.get(True))["workflow_dispatch"]["inputs"]  # PyYAML reads `on` as True
    assert set(inputs) == {"rebuild", "full"}
    for box in inputs.values():
        assert (box["type"], box["default"]) == ("boolean", False)
        assert f"'{box['description']}'" in update.HOW_TO_PROCEED


def test_the_update_job_outlasts_a_run_on_the_default_limits():
    # No call starts after the time budget, but one started just before it may take its whole timeout.
    timeout = _job(_workflow("update.yml"))["timeout-minutes"]
    assert timeout > update.DEFAULT_TIME_BUDGET + analyze.CONSOLIDATE_TIMEOUT / 60


def test_the_website_waits_for_queued_builds_and_skips_an_update_that_changed_nothing():
    pages = _workflow("pages.yml")
    assert pages["concurrency"]["cancel-in-progress"] is False
    build = pages["jobs"]["build"]
    assert build["needs"] == "gate" and build["if"] == "needs.gate.outputs.changed == 'true'"


# ---------------------------------------------------------------- the audit (audit.yml, route-audit.sh)

TODAY = "2026-10-09"


def _route_audit(repo: Repo, mode: str, branch: str = "main", today: str = TODAY, data: str | None = None,
                 report: str | None = None, files: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
    """route-audit.sh in a fresh checkout of `branch`, after audit.py wrote `data`, `report` and `files` (None:
    removed)."""
    work = repo.clone(branch)
    if data is not None:
        (work / "data.json").write_text(data + "\n")
    if report is not None:
        (work / "audit-report.md").write_text(report)
    for name, content in (files or {}).items():
        if content is None:
            (work / name).unlink()
        else:
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
    return subprocess.run(["bash", str(AUDIT_SCRIPT), mode], cwd=work, env={**repo.env, "AUDIT_TODAY": today},
                          capture_output=True, text=True, check=False)


def test_the_audit_workflow_is_manual_reviewed_and_pinned_like_the_monthly_run():
    audit_yml, update_yml = _workflow("audit.yml"), _workflow("update.yml")
    assert set(audit_yml.get("on", audit_yml.get(True))) == {"workflow_dispatch"}  # PyYAML reads `on` as True
    job = _job(audit_yml)
    # Its own concurrency group: a queued audit never cancels a pending monthly update.
    assert audit_yml["concurrency"]["group"] != update_yml["concurrency"]["group"]
    check = _step(job, "Check that no update runs and no other audit is open")
    assert "gh run list --workflow update.yml" in check["run"] and "route-audit.sh check" in check["run"]
    # Nothing is paid before the checks, and both workflows get the CLI from the action that pins it.
    assert _in_order(job, check["name"], CLI_ACTION, "audit") and _in_order(_job(update_yml), CLI_ACTION, "update")
    # The result is routed to review whatever the audit step ended with, timed out included, and never published.
    route = _step(job, "Send the result to a pull request")
    assert route["run"] == "bash .github/scripts/route-audit.sh route"
    assert route["if"] == "${{ !cancelled() && steps.audit.outcome != 'skipped' }}"
    assert not any("route-update.sh" in step.get("run", "") or "git push" in step.get("run", "")
                   for step in job["steps"])
    # The time budget, a call still running at its end and the routing fit into the job.
    budget = int(job["env"]["TIME_BUDGET"])
    assert budget == audit.DEFAULT_TIME_BUDGET
    assert budget + audit.PROPOSE_TIMEOUT / 60 < _step(job, "audit")["timeout-minutes"] < job["timeout-minutes"] - 5


@pytest.mark.parametrize(("propose_code", "budget", "applied", "code"), [
    (0, "75", True, 0), (1, "75", False, 1), (0, "0", False, 1)])
def test_the_workflow_applies_a_complete_proposal_within_the_time_left(tmp_path, propose_code, budget, applied, code):
    step = _step(_job(_workflow("audit.yml")), "audit")
    calls = _run_step(tmp_path, step, 0, MAX_COST="25", CATEGORIES="okonomi,dommere", TIME_BUDGET=budget,
                      PROPOSE_CODE=str(propose_code))
    assert calls[0] == f"run audit.py propose --max-cost 25 --time-budget {budget} --categories okonomi,dommere"
    assert calls[1:] == (["run audit.py apply --max-cost 25 --time-budget 75"] if applied else [])
    assert (tmp_path / "output").read_text() == f"code={code}\n"


def test_a_new_audit_waits_for_an_open_one_and_for_the_monthly_update(repo):
    assert _route_audit(repo, "check").returncode == 0
    month_end = _route_audit(repo, "check", today="2026-10-30")
    assert month_end.returncode == 1 and "The month ends within two days" in month_end.stderr
    assert _route_audit(repo, "check", today="2026-10-29").returncode == 0
    repo.commit_elsewhere(f"auto/audit-{TODAY}", "answers.json", "{}\n")  # an audit cut off earlier today
    again = _route_audit(repo, "check")
    assert again.returncode == 1 and f"An audit is open on auto/audit-{TODAY}" in again.stderr
    assert _route_audit(repo, "check", branch=f"auto/audit-{TODAY}").returncode == 0  # a run that finishes it


@needs_jq
def test_an_audit_goes_to_a_pull_request_and_never_to_main(repo):
    main, branch = repo.git("rev-parse", "main"), f"auto/audit-{TODAY}"
    cut_off = _route_audit(repo, "route", data='{"audited": 1}', report=f"{audit.UNFINISHED}\n# Rule audit\n")
    assert cut_off.returncode == 0, cut_off.stderr
    assert repo.git("rev-parse", "main") == main and repo.file(branch) == '{"audited": 1}'
    (pr,) = repo.prs()
    assert (pr["headRefName"], pr["baseRefName"], pr["body"]) == (branch, "main", f"{audit.UNFINISHED}\n# Rule audit\n")
    assert repo.gh_calls()[-1]["args"][-3] == f"Rule audit {TODAY} (unfinished)"
    assert "audit-report.md" not in repo.git("ls-tree", "--name-only", branch)

    # A run on the audit's branch finishes it: it commits there and updates its pull request.
    first = repo.git("rev-parse", branch)
    finished = _route_audit(repo, "route", branch=branch, today="2026-10-10", data='{"audited": 2}',
                            report=f"{audit.APPLIED}\n# Finished\n")
    assert finished.returncode == 0, finished.stderr
    assert repo.git("rev-parse", f"{branch}~1") == first and repo.file(branch) == '{"audited": 2}'
    (pr,) = repo.prs()
    assert (pr["baseRefName"], pr["body"]) == ("main", f"{audit.APPLIED}\n# Finished\n")
    edited = repo.gh_calls()[-1]["args"]
    assert edited[edited.index("--title") + 1] == f"Rule audit {TODAY}" and repo.git("rev-parse", "main") == main


def test_a_new_audit_does_not_start_when_origin_cannot_be_asked(repo):
    work = repo.clone("main")
    repo.git("remote", "set-url", "origin", str(repo.root / "gone.git"), cwd=work)
    result = subprocess.run(["bash", str(AUDIT_SCRIPT), "check"], cwd=work, env={**repo.env, "AUDIT_TODAY": TODAY},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "Cannot list origin's audit branches" in result.stderr


@needs_jq
def test_an_unfinished_audit_commits_what_it_paid_for_and_no_rule(repo):
    work = repo.clone("main")
    for name, content in (("data/regler/okonomi.json", "{}\n"), ("data/slugs.json", "{}\n"),
                          ("regelsaet/2026.md", "# 2026\n")):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content)
    repo.git("add", "-A", cwd=work)
    repo.git("-c", "user.name=Someone", "-c", "user.email=someone@example.com", "commit", "-q", "-m", "data", cwd=work)
    repo.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    branch = f"auto/audit-{TODAY}"
    half = {"data/regler/okonomi.json": '{"half": 1}\n', "data/regler/antidoping.json": "{}\n",
            "data/slugs.json": '{"half": 1}\n', "regelsaet/2026.md": None,
            "data/regler_ops.json": '{"applied": {"time": "now"}}\n', "data/audit/propose-okonomi-1.json": "{}\n"}
    result = _route_audit(repo, "route", report=f"{audit.UNFINISHED}\n# Cut off\n", files=half)
    assert result.returncode == 0, result.stderr
    committed = set(repo.git("ls-tree", "-r", "--name-only", branch).split())
    assert {"data/audit/propose-okonomi-1.json", "regelsaet/2026.md"} <= committed
    assert not {"data/regler/antidoping.json", "data/regler_ops.json"} & committed  # its ops file claims applied
    assert repo.file(branch, "data/regler/okonomi.json") == "{}" and repo.file(branch, "data/slugs.json") == "{}"


@needs_jq
@pytest.mark.parametrize(("report", "ending"), [
    (None, "audit.py wrote no report; the run's log has what it did.\n"),
    ("".join(f"- op {i}\n" for i in range(10_000)), "The report is cut short here; data/regler_ops.json has every op.\n")])
def test_an_audit_pull_request_says_where_a_missing_or_long_report_is(repo, report, ending):
    result = _route_audit(repo, "route", data='{"audited": 1}', report=report)
    assert result.returncode == 0, result.stderr
    body = repo.prs()[-1]["body"]
    assert len(body.encode()) < 65_536 and body.endswith(ending)


def test_a_result_is_on_its_review_branch_before_its_pull_request_is_made(repo):
    # The body fails to build (set -u: a run link without its server URL), so no pull request is made; what the run
    # paid for is on origin all the same.
    repo.env["GITHUB_RUN_ID"] = "1"
    repo.env.pop("GITHUB_SERVER_URL", None)
    assert repo.route(3).returncode != 0 and repo.file("auto/update") == '{"new": 1}'
    assert _route_audit(repo, "route", data='{"audited": 1}').returncode != 0
    assert repo.file(f"auto/audit-{TODAY}") == '{"audited": 1}' and repo.gh_calls() == []
