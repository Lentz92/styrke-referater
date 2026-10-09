#!/usr/bin/env bash
# Send the result of `uv run update.py` where it belongs, by its exit code:
#   0      the checks passed: commit to the branch the run started from (main for the monthly run), and close
#          the review pull request a previous run left open there;
#   3      the checks found errors: commit to the review branch and open or update its pull request;
#   other  the run failed: keep its partial results like 0 when its run-report.md says the checks passed (a
#          report from a run that got that far), else like 3.
# A result that cannot be pushed to its branch (it moved during the run) goes to review too. Run from the
# repository root after update.py, with GH_TOKEN set for gh. Safe to repeat: the review branch is force-pushed
# from this run's working tree, so there is one branch and at most one open pull request per base branch.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pr.sh"

code=${1:?usage: route-update.sh EXIT_CODE_OF_UPDATE_PY}
report=${RUN_REPORT:-run-report.md}
checks_passed="<!-- checks: passed -->"  # update.py's CHECKS_PASSED
title="Monthly update needs review"

base=$(git rev-parse --abbrev-ref HEAD)
if [ "$base" = HEAD ]; then
  echo "::error::Not on a branch; check out the branch the update ran on." >&2
  exit 1
fi
# A run on another branch (a test via "Run workflow") must not touch the pull request for main.
review_branch=auto/update
if [ "$base" != main ]; then
  review_branch="auto/update-$base"
fi

case $code in
  0) route=publish ;;
  3) route=review ;;
  *) if [ -f "$report" ] && head -n 1 "$report" | grep -qxF "$checks_passed"; then route=publish; else route=review; fi ;;
esac

stage_result
today=$(date -u +%Y-%m-%d)

if git diff --cached --quiet; then
  if [ "$code" = 3 ]; then
    echo "::error::The checks found errors, but the run changed nothing: they are in $base already. Fix data/ there (see $report)." >&2
    exit 1
  fi
  echo "No changes to commit."
  exit 0
fi

note=""
if [ "$route" = publish ]; then
  git commit -q -m "Monthly rule overview update $today"
  if git push -q origin "HEAD:refs/heads/$base"; then
    echo "Committed the update to $base."
    number=$(open_pr "$review_branch")
    if [ -n "$number" ]; then
      gh pr close "$number" --comment "Superseded: a later run published its update to $base directly."
      git push -q origin --delete "$review_branch" || echo "::warning::Could not delete $review_branch."
    fi
    exit 0
  fi
  # Someone pushed to the branch during the run. The result is still worth keeping, so it goes to review.
  echo "::warning::$base changed during the run; sending the result to $review_branch instead."
  git commit -q --amend -m "Monthly rule overview update $today (needs review)"
  note="**$base changed while this run was going**, so its result could not be pushed there. Merge this if it still applies."
else
  git commit -q -m "Monthly rule overview update $today (needs review)"
fi

body=$(mktemp)
if [ -n "$note" ]; then
  printf '%s\n\n' "$note" > "$body"
fi
pr_body "$report" "the run page has all of it." "update.py wrote no run report; the run's log has what it found." \
  >> "$body"
send_to_review "$review_branch" "$base" "$title" "$body"
