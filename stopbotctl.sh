#!/bin/bash
# stopbot/stopbotctl.sh
#
# Easy start/stop/status control for the stopbot background job, so you
# don't have to remember the launchctl path and plist filename.
#
# Usage:
#   ./stopbotctl.sh start     # start the background job
#   ./stopbotctl.sh stop      # stop it
#   ./stopbotctl.sh restart   # stop then start (use after editing watcher.py)
#   ./stopbotctl.sh status    # is it running right now?
#   ./stopbotctl.sh log       # tail the log live (Ctrl+C to stop watching)
#
# Tip: add this to your ~/.zshrc so you can just type "stopbot start"
# from anywhere instead of the full path:
#   alias stopbot="~/projects/personal/stopbot/stopbotctl.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.user.stopbot.plist"
LOG_FILE="$SCRIPT_DIR/stopbot.log"

case "$1" in
  start)
    if [ ! -f "$PLIST" ]; then
      echo "Not installed yet. Run ./install_launchd.sh first."
      exit 1
    fi
    if launchctl list | grep -q com.user.stopbot; then
      echo "stopbot is already running."
    else
      launchctl load "$PLIST"
      echo "stopbot started."
    fi
    ;;
  stop)
    if launchctl list | grep -q com.user.stopbot; then
      launchctl unload "$PLIST"
      echo "stopbot stopped."
    else
      echo "stopbot wasn't running."
    fi
    ;;
  restart)
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "stopbot restarted (picks up any changes to watcher.py)."
    ;;
  status)
    if launchctl list | grep -q com.user.stopbot; then
      echo "stopbot is RUNNING."
    else
      echo "stopbot is STOPPED."
    fi
    ;;
  log)
    echo "Tailing $LOG_FILE — Ctrl+C to stop watching (this does not stop stopbot itself)."
    tail -f "$LOG_FILE"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log}"
    exit 1
    ;;
esac
