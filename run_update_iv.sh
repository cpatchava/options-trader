#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="$(which python3)"
cd "$DIR"
"$PYTHON" update_iv.py >> "$DIR/iv_update.log" 2>&1
