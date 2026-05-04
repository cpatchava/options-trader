#!/bin/bash
# Setup script for Raspberry Pi deployment of options-trader.
# Run this once on the Pi after copying the project over.
# Usage: bash setup_pi.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON=$(which python3)
LOG_DIR="$PROJECT_DIR"

echo "=== Options Trader — Raspberry Pi Setup ==="
echo "Project: $PROJECT_DIR"
echo "Python:  $PYTHON ($($PYTHON --version))"
echo ""

# ── 1. Timezone ───────────────────────────────────────────────────────────────
echo "[1/5] Setting timezone to America/New_York..."
sudo timedatectl set-timezone America/New_York
echo "      Done. $(timedatectl | grep 'Time zone')"

# ── 2. System packages ────────────────────────────────────────────────────────
echo "[2/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv libopenblas-dev
echo "      Done."

# ── 3. Python dependencies ────────────────────────────────────────────────────
echo "[3/5] Installing Python packages..."
$PYTHON -m pip install --upgrade pip --quiet
$PYTHON -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet
echo "      Done."

# ── 4. Data directory ─────────────────────────────────────────────────────────
echo "[4/5] Ensuring data directory exists..."
mkdir -p "$PROJECT_DIR/data"
echo "      Done."

# ── 5. Crontab ────────────────────────────────────────────────────────────────
echo "[5/5] Installing crontab (Mon-Fri, market hours ET)..."

# Write crontab entries — check if already present first
CRON_REPORT="0 8 * * 1-5 $PROJECT_DIR/run_report.sh"
CRON_PAPER="30 16 * * 1-5 $PROJECT_DIR/run_paper_trading.sh"

# Make shell scripts point to system python3
sed -i "s|/usr/local/Cellar/python@3.13/3.13.7/bin/python3.13|$PYTHON|g" \
    "$PROJECT_DIR/run_report.sh" \
    "$PROJECT_DIR/run_paper_trading.sh"

# Install cron entries (preserving any existing ones)
( crontab -l 2>/dev/null | grep -v "options-trader"; \
  echo "$CRON_REPORT"; \
  echo "$CRON_PAPER" ) | crontab -

echo "      Crontab installed:"
crontab -l | grep "options-trader"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env file:          scp ~/.env pi@<pi-ip>:$PROJECT_DIR/.env"
echo "  2. Copy existing paper trades:   scp -r ~/dev/options-trader/data/ pi@<pi-ip>:$PROJECT_DIR/data/"
echo "  3. Verify cron is running:       sudo service cron status"
echo "  4. Test manually:                python3 $PROJECT_DIR/daily_report.py"
echo ""
echo "Cron schedule (all times ET):"
echo "  08:00  Mon-Fri  Morning screener report  → $PROJECT_DIR/cron.log"
echo "  16:30  Mon-Fri  Paper trading engine     → $PROJECT_DIR/paper_trading.log"
