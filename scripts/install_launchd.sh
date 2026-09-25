#!/bin/zsh
# Installs a launchd agent that runs JobHub every day at 07:30. Re-run to update. Uninstall with:
#   launchctl bootout gui/$(id -u)/com.jobhub.daily && rm ~/Library/LaunchAgents/com.jobhub.daily.plist
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jobhub.daily.plist"
HOUR="${1:-7}"; MINUTE="${2:-30}"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobhub.daily</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$ROOT/scripts/run_daily.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/data/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/launchd.err</string>
</dict></plist>
PL
chmod +x "$ROOT/scripts/run_daily.sh"
launchctl bootout "gui/$(id -u)/com.jobhub.daily" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed com.jobhub.daily (daily at $HOUR:$(printf %02d $MINUTE)); run now with: launchctl kickstart gui/$(id -u)/com.jobhub.daily"
