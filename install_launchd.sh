#!/bin/bash
# Installs stopbot as a background launchd job so it runs automatically
# at login and restarts if it crashes.
#
# Usage:
#   chmod +x install_launchd.sh
#   ./install_launchd.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.user.stopbot.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.stopbot.plist"
PYTHON_PATH="$(which python3)"

echo "Installing stopbot from $SCRIPT_DIR"
echo "Using python3 at: $PYTHON_PATH"
echo ""
echo "IMPORTANT: make sure THIS exact path has Full Disk Access:"
echo "  System Settings -> Privacy & Security -> Full Disk Access -> +"
echo "  Press Cmd+Shift+G and paste: $PYTHON_PATH"
echo ""

# Fill in the real python path and script folder in place of the placeholders.
sed -e "s|REPLACE_WITH_FULL_PATH|$SCRIPT_DIR|g" -e "s|REPLACE_WITH_PYTHON_PATH|$PYTHON_PATH|g" "$PLIST_SRC" > "$PLIST_DEST"

# Unload any previous version, then load the new one.
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo "Installed and started. Logs:"
echo "  $SCRIPT_DIR/stopbot.log       (app-level log)"
echo "  $SCRIPT_DIR/stopbot.out.log   (stdout)"
echo "  $SCRIPT_DIR/stopbot.err.log   (stderr)"
echo ""
echo "To stop it:   launchctl unload $PLIST_DEST"
echo "To restart:   launchctl load $PLIST_DEST"
