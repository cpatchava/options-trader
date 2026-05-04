#!/bin/bash
# Runs the daily options report and logs output.
# Invoked by crontab Mon-Fri at 8 AM ET.
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="$(which python3)"
cd "$DIR"
"$PYTHON" daily_report.py >> "$DIR/cron.log" 2>&1
