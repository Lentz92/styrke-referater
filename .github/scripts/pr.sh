# What route-update.sh and route-audit.sh share: committing as github-actions[bot] and sending a result to a review
# branch and its pull request. Sourced, so it runs with the caller's `set -euo pipefail`, from the repository root.

# Stage everything the run changed, for a commit by github-actions[bot].
stage_result() {
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A
}

# The number of BRANCH's open pull request, or nothing. Forks may have a branch of the same name; only this
# repository's counts.
open_pr() {
  gh pr list --head "$1" --state open --json number,isCrossRepository \
    --jq 'map(select(.isCrossRepository | not)) | .[0].number // empty'
}

# A pull request body on stdout: REPORT, or NO_REPORT when the run wrote none, then the link to this run. A report too
# long for a pull request is cut, ending with "The report is cut short here; " and FULL_REPORT_AT.
pr_body() {
  local report=$1 full_report_at=$2 no_report=$3
  local max_body=60000  # GitHub refuses a pull request body over 65536 characters
  if [ -s "$report" ]; then
    if [ "$(wc -c < "$report")" -gt "$max_body" ]; then
      head -c "$max_body" "$report" | sed '$d'  # drop the last line, which may be cut short
      printf '\n…\n\nThe report is cut short here; %s\n' "$full_report_at"
    else
      cat "$report"
    fi
  else
    echo "$no_report"
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    printf '\nRun: %s/%s/actions/runs/%s\n' "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID"
  fi
}

# Force-push HEAD to BRANCH, then open its pull request into BASE with TITLE and the body in BODY_FILE, or update the
# open one. Forced: the branch holds this run's result only, so a repeated run replaces it.
send_to_review() {
  local branch=$1 base=$2 title=$3 body=$4 number
  git push -q --force origin "HEAD:refs/heads/$branch"
  number=$(open_pr "$branch")
  if [ -n "$number" ]; then
    gh pr edit "$number" --base "$base" --title "$title" --body-file "$body"
    echo "Updated pull request #$number with this run's result."
  else
    gh pr create --head "$branch" --base "$base" --title "$title" --body-file "$body"
  fi
}
