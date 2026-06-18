#!/bin/bash
# Runs the real-trade report (Google Sheets) and logs output.
# Invoked by crontab Mon-Fri at 10 AM ET alongside the paper report.
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="$(which python3)"
cd "$DIR"
"$PYTHON" real_report.py >> "$DIR/real_report.log" 2>&1
