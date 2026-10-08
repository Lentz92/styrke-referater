"""The monthly workflow's routing (.github/scripts/route-update.sh), run against a local origin with a fake gh."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import update

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "route-update.sh"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the fake gh applies --jq with jq")

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
        self.commit_elsewhere("main", ".gitignore", "run-report.md\n")
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
        return subprocess.run(["bash", str(SCRIPT), str(code)], cwd=work, env=self.env, capture_output=True,
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


PASSED = f"{update.CHECKS_PASSED}\n# Report\n"


def test_the_script_reads_the_markers_update_writes():
    assert f'checks_passed="{update.CHECKS_PASSED}"' in SCRIPT.read_text()


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


def test_a_result_that_cannot_be_pushed_to_main_goes_to_review(repo):
    result = repo.route(0, race=True)
    assert result.returncode == 0, result.stderr
    assert repo.file("main", "other.txt") == "pushed during the run" and repo.file("main") == "{}"
    assert repo.file("auto/update") == '{"new": 1}'
    (pr,) = repo.prs()
    assert pr["body"].startswith("**main changed while this run was going**")


def test_a_run_on_another_branch_has_its_own_review_branch(repo):
    repo.add_pr(number=3, headRefName="auto/update", baseRefName="main", body="main's review")
    repo.commit_elsewhere("feature", "feature.txt", "x\n")

    result = repo.route(3, branch="feature")
    assert result.returncode == 0, result.stderr
    assert repo.file("auto/update-feature") == '{"new": 1}' and "auto/update" not in repo.branches()
    main_review, feature_review = repo.prs()
    assert (main_review["baseRefName"], main_review["body"]) == ("main", "main's review")
    assert (feature_review["headRefName"], feature_review["baseRefName"]) == ("auto/update-feature", "feature")


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


def test_a_pull_request_from_a_fork_is_not_the_review(repo):
    repo.add_pr(number=3, headRefName="auto/update", baseRefName="main", isCrossRepository=True, body="fork")

    result = repo.route(3)
    assert result.returncode == 0, result.stderr
    fork, review = repo.prs()
    assert fork["body"] == "fork" and review["headRefName"] == "auto/update" and review["number"] == 4


def test_a_long_report_is_cut_to_fit_a_pull_request(repo):
    result = repo.route(3, report="".join(f"- error {i}\n" for i in range(10_000)))
    assert result.returncode == 0, result.stderr
    body = repo.prs()[-1]["body"]
    assert len(body.encode()) < 65_536 and body.endswith("the run page has all of it.\n")
