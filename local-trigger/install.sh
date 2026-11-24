#!/bin/bash
# Installs (or reinstalls) the Mac launchd job that backs up GitHub's unreliable timer.
# Run once from the repo:   bash local-trigger/install.sh
# Remove it:                bash local-trigger/install.sh --uninstall
set -eu
LABEL="local.market-read-trigger"
DIR="$HOME/Library/Application Support/market-read-trigger"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/market-read-trigger.log"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PLIST"; rm -rf "$DIR"; echo "Removed $LABEL."; exit 0
fi

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
cp "$(dirname "$0")/dispatch.sh" "$DIR/dispatch.sh"
chmod +x "$DIR/dispatch.sh"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/dispatch.sh</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL: checks every 15 minutes (and on wake). Log: $LOG"
