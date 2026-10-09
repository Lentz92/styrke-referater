#!/usr/bin/env bash
# The audit workflow's branch handling (.github/workflows/audit.yml), run from the repository root.
#
#   route-audit.sh check   before any Claude call: fail when another audit's branch is on origin (its pull request
#                          holds answers already paid for, which a new audit's branch of the same day would overwrite)
#                          or when the month ends within two days (the monthly update runs on the 1st, and an open
#                          audit pull request conflicts with it in data/regler/).
#   route-audit.sh route   after `uv run audit.py propose` and `apply`, finished or cut off: commit everything they
#                          changed (the ops file, the answers kept in data/audit/, data/runs.jsonl, and once applied
#                          data/regler/, data/slugs.json and regelsaet/) to auto/audit-<date> and open its pull request
#                          into the branch the audit ran on, or update the open one, with audit-report.md as the body.
#
# An audit changes earlier years by design, so it is never pushed to the branch it ran on: merging the pull request
# publishes it. A run on an auto/audit-* branch itself (finishing a cut-off audit, whose kept answers are on that
# branch) commits there and updates its pull request into main. GH_TOKEN must be set for gh; AUDIT_TODAY (YYYY-MM-DD)
# replaces today's date in tests.
set -euo pipefail

mode=${1:?usage: route-audit.sh check|route}
report=${AUDIT_REPORT:-audit-report.md}
applied_marker="<!-- audit: applied -->"  # audit.py's APPLIED
max_body=60000  # GitHub refuses a pull request body over 65536 characters
today=${AUDIT_TODAY:-$(date -u +%Y-%m-%d)}

current=$(git rev-parse --abbrev-ref HEAD)
if [ "$current" = HEAD ]; then
  echo "::error::Not on a branch; check out the branch the audit runs on." >&2
  exit 1
fi
case $current in
  auto/audit-*) review_branch=$current; base=main ;;
  *) review_branch="auto/audit-$today"; base=$current ;;
esac

if [ "$mode" = check ]; then
  month_end=$(python3 -c 'import datetime as d, sys; t = d.date.fromisoformat(sys.argv[1]); print(int((t + d.timedelta(days=2)).month != t.month))' "$today")
  if [ "$month_end" = 1 ]; then
    echo "::error::The month ends within two days: the monthly update runs on the 1st, and an open audit pull request would conflict with it. Run the audit after the 1st's update." >&2
    exit 1
  fi
  if ! heads=$(git ls-remote --heads origin 'auto/audit-*'); then
    echo "::error::Cannot list origin's audit branches, so cannot tell whether another audit is open; try again." >&2
    exit 1
  fi
  open=$(printf '%s\n' "$heads" | sed 's|.*refs/heads/||' | { grep -vxF -e "$current" -e '' || true; } | paste -sd ' ' -)
  if [ -n "$open" ]; then
    echo "::error::An audit is open on $open: its pull request holds answers already paid for. Run this workflow on that branch to finish it, or merge or close its pull request and delete the branch, then start a new audit." >&2
    exit 1
  fi
  exit 0
fi

if [ "$mode" != route ]; then
  echo "usage: route-audit.sh check|route" >&2
  exit 2
fi
title="Rule audit ${review_branch#auto/audit-}"
if ! { [ -f "$report" ] && head -n 1 "$report" | grep -qxF "$applied_marker"; }; then
  title="$title (unfinished)"
  # Not applied, or cut off while applying: the rules, the slug history and the pages stay as they were, so a
  # half-applied state is never committed, and neither is an ops file that says it was applied. What was paid for
  # (data/audit/, the ops file, data/runs.jsonl) is kept.
  for path in data/regler data/slugs.json regelsaet; do
    if git cat-file -e "HEAD:$path" 2>/dev/null; then git checkout -q HEAD -- "$path"; fi
  done
  git clean -fdq -- data/regler regelsaet
  ops=data/regler_ops.json
  if [ -f "$ops" ] && python3 -c 'import json, sys; sys.exit(not json.load(open(sys.argv[1])).get("applied"))' "$ops"; then
    if git cat-file -e "HEAD:$ops" 2>/dev/null; then git checkout -q HEAD -- "$ops"; else rm "$ops"; fi
  fi
fi

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
