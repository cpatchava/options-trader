"""
Reads data/trades.csv and computes portfolio stats.
No manual entry here — just edit the CSV directly.

CSV columns:
  open_date     : YYYY-MM-DD
  ticker        : e.g. XOM
  option_type   : put or call
  strike        : e.g. 143.20
  expiry        : YYYY-MM-DD
  contracts     : integer (each = 100 shares)
  open_premium  : credit received per share when selling (e.g. 1.94)
  close_date    : YYYY-MM-DD  (leave blank until closed)
  close_premium : debit paid to close per share; 0 if expired worthless (leave blank until closed)
  outcome       : expired | assigned | closed  (leave blank until closed)
  notes         : anything useful

P&L per trade = (open_premium - close_premium) * 100 * contracts - (0.65 * contracts * 2)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date

TRADES_FILE = Path(__file__).parent / 'data' / 'trades.csv'
COMMISSION = 0.65  # per contract per leg


def load_trades() -> pd.DataFrame:
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(TRADES_FILE, parse_dates=['open_date', 'expiry'])
    # close columns may be partially empty
    if 'close_date' in df.columns:
        df['close_date'] = pd.to_datetime(df['close_date'], errors='coerce')
    if 'close_premium' in df.columns:
        df['close_premium'] = pd.to_numeric(df['close_premium'], errors='coerce')
    df['open_premium'] = pd.to_numeric(df['open_premium'], errors='coerce')
    df['contracts'] = pd.to_numeric(df['contracts'], errors='coerce').fillna(1).astype(int)
    return df


def open_positions(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where close_date is not yet filled in."""
    if df.empty:
        return df
    return df[df['close_date'].isna()].reset_index(drop=True)


def assigned_shares(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns open 'shares' rows — positions where a put was assigned and the
    user is now holding stock.

    Two ways these appear in trades.csv:
      1. A row with option_type='shares' that has no close_date yet.
      2. A put row with outcome='assigned' — legacy: we infer from this too
         if no corresponding shares row exists for that ticker.
    """
    if df.empty:
        return pd.DataFrame()

    # Explicit shares rows (preferred going forward)
    explicit = df[(df['option_type'].str.lower() == 'shares') &
                  df['close_date'].isna()].copy()

    # Implicit: assigned puts without a matching explicit shares row
    assigned_puts = df[
        (df['option_type'].str.lower() == 'put') &
        (df['outcome'].fillna('').str.lower() == 'assigned')
    ].copy()

    explicit_tickers = set(explicit['ticker'].tolist()) if not explicit.empty else set()
    implicit = assigned_puts[~assigned_puts['ticker'].isin(explicit_tickers)].copy()

    if explicit.empty and implicit.empty:
        return pd.DataFrame()

    parts = [p for p in [explicit, implicit] if not p.empty]
    return pd.concat(parts, ignore_index=True)


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where close_date is present."""
    if df.empty:
        return df
    return df[df['close_date'].notna()].copy()


def compute_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """Add a pnl column to closed trades."""
    closed = closed_trades(df)
    if closed.empty:
        return closed
    closed['close_premium'] = closed['close_premium'].fillna(0)
    commission = COMMISSION * closed['contracts'] * 2  # open + close legs
    closed['pnl'] = (
        (closed['open_premium'] - closed['close_premium'])
        * 100 * closed['contracts']
        - commission
    )
    return closed


def summary(starting_capital: float = 100_000) -> dict:
    df = load_trades()
    if df.empty:
        return {
            'open_positions': 0, 'closed_trades': 0,
            'mtd_collected': 0.0, 'total_collected': 0.0,
            'win_rate_pct': 0.0, 'prediction_accuracy_pct': None,
            'starting_capital': starting_capital,
        }

    closed = compute_pnl(df)
    open_pos = open_positions(df)

    total_pnl = float(closed['pnl'].sum()) if not closed.empty else 0.0

    # MTD: trades closed this calendar month
    today = date.today()
    if not closed.empty:
        mtd = closed[
            (closed['close_date'].dt.year == today.year) &
            (closed['close_date'].dt.month == today.month)
        ]
        mtd_pnl = float(mtd['pnl'].sum())
    else:
        mtd_pnl = 0.0

    win_rate = 0.0
    if not closed.empty and len(closed) > 0:
        win_rate = (closed['pnl'] > 0).mean() * 100

    return {
        'open_positions': len(open_pos),
        'closed_trades': len(closed),
        'mtd_collected': round(mtd_pnl, 2),
        'total_collected': round(total_pnl, 2),
        'win_rate_pct': round(win_rate, 1),
        'starting_capital': starting_capital,
    }


def print_summary():
    df = load_trades()
    s = summary()

    print("\nPORTFOLIO SUMMARY")
    print(f"  Open positions  : {s['open_positions']}")
    print(f"  Closed trades   : {s['closed_trades']}")
    print(f"  MTD P&L         : ${s['mtd_collected']:,.2f}")
    print(f"  Total P&L       : ${s['total_collected']:,.2f}")
    print(f"  Win rate        : {s['win_rate_pct']}%")

    open_pos = open_positions(df)
    if not open_pos.empty:
        print("\nOPEN POSITIONS")
        print(f"  {'#':<4} {'Ticker':<6} {'Type':<5} {'Strike':>7} {'Expiry':<12} {'Prem':>6} {'Cts':>4} {'Pred P&L':>10}")
        print("  " + "-" * 62)
        for i, row in open_pos.iterrows():
            pred = (row['open_premium'] * 100 * row['contracts']
                    - COMMISSION * row['contracts'] * 2)
            exp_str = row['expiry'].strftime('%Y-%m-%d') if pd.notna(row['expiry']) else ''
            print(f"  {i+1:<4} {row['ticker']:<6} {str(row['option_type']).upper():<5} "
                  f"${row['strike']:>6.2f}  {exp_str:<12} "
                  f"${row['open_premium']:>5.2f}  {row['contracts']:>4}  "
                  f"${pred:>9.0f}")
    else:
        print("\n  No open positions.")

    closed = compute_pnl(df)
    if not closed.empty:
        print("\nRECENT CLOSED TRADES")
        recent = closed.sort_values('close_date', ascending=False).head(10)
        print(f"  {'Ticker':<6} {'Type':<5} {'Strike':>7} {'Expiry':<12} {'Outcome':<10} {'P&L':>9}")
        print("  " + "-" * 56)
        for _, row in recent.iterrows():
            print(f"  {row['ticker']:<6} {str(row['option_type']).upper():<5} "
                  f"${row['strike']:>6.2f}  {str(row['expiry'].date()):<12} "
                  f"{str(row['outcome']).lower():<10} ${row['pnl']:>8.0f}")


if __name__ == '__main__':
    print_summary()
