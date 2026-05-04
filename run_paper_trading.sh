#!/bin/bash
# Runs the paper trading engine at market close and logs output.
# Invoked by crontab Mon-Fri at 4:30 PM ET (1:30 PM PT).
cd /Users/chaitanyapatchava/dev/options-trader
/usr/local/Cellar/python@3.13/3.13.7/bin/python3.13 paper_trading.py >> /Users/chaitanyapatchava/dev/options-trader/paper_trading.log 2>&1
