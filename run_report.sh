#!/bin/bash
# Runs the daily options report and logs output.
# Invoked by crontab Mon-Fri at 8 AM.
cd /Users/chaitanyapatchava/dev/options-trader
/usr/local/Cellar/python@3.13/3.13.7/bin/python3.13 daily_report.py >> /Users/chaitanyapatchava/dev/options-trader/cron.log 2>&1
