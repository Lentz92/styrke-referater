#!/usr/bin/env bash
# Send the result of `uv run audit.py propose` and `apply` to review: commit everything they changed (the ops file,
# the answers kept in data/audit/, and once applied data/regler/, data/slugs.json and regelsaet/) to the branch
# auto/audit-<date> and open its pull request into the branch the audit ran on, or update the open one, with
# audit-report.md as the body. An audit changes earlier years by design, so it is never pushed to the branch it ran
# on: merging the pull request publishes it. A run on an auto/audit-* branch itself (finishing a cut-off audit, whose
# kept answers are on that branch) commits there and updates its pull request into main. Run from the repository
# root after audit.py, with GH_TOKEN set for gh.
set -euo pipefail

report=${AUDIT_REPORT:-audit-report.md}
max_body=60000  # GitHub refuses a pull request body over 65536 characters

base=$(git rev-parse --abbrev-ref HEAD)
if [ "$base" = HEAD ]; then
  echo "::error::Not on a branch; check out the branch the audit ran on." >&2
  exit 1
fi
case $base in
  auto/audit-*) review_branch=$base; base=main ;;
  *) review_branch="auto/audit-$(date -u +%Y-%m-%d)" ;;
esac
title="Rule audit ${review_branch#auto/audit-}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "The audit changed nothing; no pull request."
  exit 0
fi
git commit -q -m "$title"
git push -q --force origin "HEAD:refs/heads/$review_branch"

body=$(mktemp)
if [ -s "$report" ]; then
  if [ "$(wc -c < "$report")" -gt "$max_body" ]; then
    head -c "$max_body" "$report" | sed '$d' > "$body"  # drop the last line, which may be cut short
    printf '\n…\n\nThe report is cut short here; data/regler_ops.json has every op.\n' >> "$body"
  else
    cat "$report" > "$body"
  fi
else
  echo "audit.py wrote no report; the run's log has what it did." > "$body"
fi
if [ -n "${GITHUB_RUN_ID:-}" ]; then
  printf '\nRun: %s/%s/actions/runs/%s\n' "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID" >> "$body"
fi

# Forks may have a branch of the same name; only this repository's counts.
number=$(gh pr list --head "$review_branch" --state open --json number,isCrossRepository \
  --jq 'map(select(.isCrossRepository | not)) | .[0].number // empty')
if [ -n "$number" ]; then
  gh pr edit "$number" --base "$base" --title "$title" --body-file "$body"
  echo "Updated pull request #$number with this audit."
else
  gh pr create --head "$review_branch" --base "$base" --title "$title" --body-file "$body"
fi
