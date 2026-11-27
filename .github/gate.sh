#!/usr/bin/env bash
# Decides whether a timed run should do the work.
# Timed runs come from GitHub's schedule or from the Mac's launchd trigger
# (workflow_dispatch with timed=true). Either can fire late, early or more than once,
# so the first timed run inside a window does the update; later ones see that the
# report was already saved and skip. A manual "Run workflow" press always runs.
#   Windows (New York time, weekdays):
#     morning   10:00-14:59  (the 10am read)
#     afternoon 15:00-23:59  (the 3pm read; a late run still saves the day's close)
set -u
out=${GITHUB_OUTPUT:-/dev/stdout}
if [ "${EVENT:-schedule}" != "schedule" ] && [ "${TIMED:-false}" != "true" ]; then
  echo "Manual run."; echo "run=true" >> "$out"; exit 0
fi

dow=$(TZ=America/New_York date +%u)
h=$(TZ=America/New_York date +%-H)
if [ "$dow" -gt 5 ]; then
  echo "Skipping: it's the weekend in New York."; echo "run=false" >> "$out"; exit 0
fi
case $h in
  10|11|12|13|14) slot=10 ;;
  15|16|17|18|19|20|21|22|23) slot=15 ;;
  *) echo "Skipping: ${h}:00 New York is outside the 10am/3pm windows."; echo "run=false" >> "$out"; exit 0 ;;
esac
since=$(date -u -d "TZ=\"America/New_York\" $(TZ=America/New_York date +%F) ${slot}:00" +%Y-%m-%dT%H:%M:%SZ)
saved=$(gh api "repos/$GITHUB_REPOSITORY/commits?sha=main&since=$since&per_page=50" \
          -q '[.[] | select(.commit.message | startswith("Market read"))] | length' 2>/dev/null || echo 0)
if [ "${saved:-0}" -gt 0 ]; then
  echo "Skipping: the ${slot}:00 report was already saved (since $since)."; echo "run=false" >> "$out"
else
  echo "Running the ${slot}:00 report."; echo "run=true" >> "$out"
fi
