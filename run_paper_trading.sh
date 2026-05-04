#!/bin/bash
# Runs the paper trading engine at market close and logs output.
# Invoked by crontab Mon-Fri at 4:30 PM ET.
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="$(which python3)"
cd "$DIR"
"$PYTHON" paper_trading.py >> "$DIR/paper_trading.log" 2>&1
