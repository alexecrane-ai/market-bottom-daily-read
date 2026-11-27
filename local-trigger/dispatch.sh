#!/bin/bash
# Run by launchd every 15 minutes on the Mac. On weekdays in the 10am and 3pm
# New York windows, it asks GitHub to run the workflow once, unless that window's
# report is already saved or a run is already going. The workflow's gate makes the
# same check, so an extra dispatch only costs a few seconds of a skipped run.
# Needs the GitHub CLI (gh), logged in with the "workflow" scope.
# DRY_RUN=1 prints what it would do without dispatching.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="alexecrane-ai/market-bottom-daily-read"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

dow=$(TZ=America/New_York date +%u)
h=$(TZ=America/New_York date +%-H)
[ "$dow" -le 5 ] || exit 0
case $h in
  10|11|12|13|14) slot=10 ;;
  15|16|17|18|19|20|21|22|23) slot=15 ;;
  *) exit 0 ;;
esac

# Start of this window in UTC (macOS/BSD date).
epoch=$(TZ=America/New_York date -j -f "%Y-%m-%d %H:%M:%S" "$(TZ=America/New_York date +%F) ${slot}:00:00" +%s)
since=$(date -u -r "$epoch" +%Y-%m-%dT%H:%M:%SZ)

saved=$(gh api "repos/$REPO/commits?sha=main&since=$since&per_page=50" \
          -q '[.[] | select(.commit.message | startswith("Market read"))] | length' 2>/dev/null) \
  || { log "Could not reach GitHub (offline or gh not logged in); will retry next tick."; exit 0; }
[ "${saved:-0}" -gt 0 ] && exit 0

active=$(gh run list -R "$REPO" -w daily.yml -L 10 --json status \
           -q '[.[] | select(.status != "completed")] | length' 2>/dev/null || echo 0)
if [ "${active:-0}" -gt 0 ]; then log "A run is already going; waiting."; exit 0; fi

if [ "${DRY_RUN:-0}" = "1" ]; then log "DRY RUN: would dispatch the ${slot}:00 report (window since $since)."; exit 0; fi
if gh workflow run daily.yml -R "$REPO" --ref main -f timed=true; then
  log "Dispatched the ${slot}:00 report."
else
  log "Dispatch failed; will retry next tick."
fi
