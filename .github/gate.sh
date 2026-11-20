#!/usr/bin/env bash
# Decides whether a timed (scheduled) run should do the work.
# GitHub's timer can fire late or skip a run, so the workflow wakes up at several
# backup times. The first wake-up inside a window does the update; later ones see
# that the report was already saved and skip.
#   Windows (New York time, weekdays): 10:00-11:59 and 15:00-16:59
set -u
out=${GITHUB_OUTPUT:-/dev/stdout}
if [ "${EVENT:-schedule}" != "schedule" ]; then echo "run=true" >> "$out"; exit 0; fi

h=$(TZ=America/New_York date +%-H)
case $h in
  10|11) slot=10 ;;
  15|16) slot=15 ;;
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
