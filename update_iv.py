"""
Standalone IV history refresh — run multiple times per day to keep
today's IV readings accurate. Each run overwrites today's stored value
so the latest mid/late-day fetch replaces any bad early-morning data.
"""

import warnings
warnings.filterwarnings('ignore')

from datetime import date
from screener import WATCHLIST, BLOCKLIST
from utils.iv_history import update as update_iv_history

if __name__ == '__main__':
    tickers = [t for t in WATCHLIST if t not in BLOCKLIST]
    print(f"IV update — {date.today()} ({len(tickers)} tickers)")
    update_iv_history(tickers, verbose=True)
    print("Done.")
