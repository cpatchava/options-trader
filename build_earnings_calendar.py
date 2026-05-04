"""
Derives a historical earnings calendar from ORATS IV crush events.

Logic: earnings always cause a sharp IV collapse the day after announcement
(the "IV crush"). We detect days where a ticker's average smoothSmvVol dropped
>25% vs the prior trading day — those prior days are the earnings dates.

Stores results in the `earnings_dates` table in options.duckdb.

Run after ingest is complete:  python build_earnings_calendar.py
Safe to re-run — drops and rebuilds the table each time.
"""

import duckdb
from pathlib import Path

DB_PATH = Path(__file__).parent / 'data' / 'options.duckdb'

# Thresholds
IV_DROP_THRESHOLD  = 0.25   # IV must drop >25% overnight to qualify
MIN_PRE_IV         = 0.15   # ignore low-IV names (< 15% IV has no meaningful crush)
MIN_OCCURRENCES    = 1      # must appear at least once to be stored


def build(con: duckdb.DuckDBPyConnection):
    print("Building earnings calendar from IV crush events...")

    # Step 1: compute per-ticker daily average IV
    con.execute("""
        CREATE OR REPLACE TEMP TABLE daily_iv AS
        SELECT
            ticker,
            trade_date,
            AVG(smoothSmvVol) AS avg_iv
        FROM options
        WHERE smoothSmvVol > 0
          AND smoothSmvVol IS NOT NULL
        GROUP BY ticker, trade_date
    """)

    # Step 2: compute day-over-day IV change using window function
    con.execute("""
        CREATE OR REPLACE TEMP TABLE iv_changes AS
        SELECT
            ticker,
            trade_date,
            avg_iv,
            LAG(avg_iv)    OVER w AS prev_iv,
            LAG(trade_date) OVER w AS prev_date
        FROM daily_iv
        WINDOW w AS (PARTITION BY ticker ORDER BY trade_date)
    """)

    # Step 3: detect IV crush days — the crush day is the day AFTER earnings,
    # so the earnings date itself is prev_date
    con.execute("""
        CREATE OR REPLACE TEMP TABLE raw_earnings AS
        SELECT
            ticker,
            prev_date                                     AS earnings_date,
            trade_date                                    AS crush_date,
            prev_iv,
            avg_iv                                        AS post_iv,
            ROUND((avg_iv - prev_iv) / prev_iv * 100, 1) AS iv_change_pct
        FROM iv_changes
        WHERE prev_iv  IS NOT NULL
          AND prev_iv  >  MIN_PRE_IV
          AND avg_iv   <  prev_iv * (1 - IV_DROP_THRESHOLD)
          -- gap between trading days must be <= 5 calendar days (no weekend gaps)
          AND DATEDIFF('day', prev_date, trade_date) <= 5
    """.replace("MIN_PRE_IV", str(MIN_PRE_IV)).replace("IV_DROP_THRESHOLD", str(IV_DROP_THRESHOLD)))

    # Step 4: deduplicate — if two crush events are within 20 days, keep the larger
    con.execute("""
        CREATE OR REPLACE TEMP TABLE deduped_earnings AS
        SELECT ticker, earnings_date, crush_date, prev_iv, post_iv, iv_change_pct
        FROM (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker,
                        -- bucket into 20-day windows
                        CAST(EPOCH(earnings_date::TIMESTAMP) / (20 * 86400) AS INTEGER)
                    ORDER BY ABS(iv_change_pct) DESC
                ) AS rn
            FROM raw_earnings
        )
        WHERE rn = 1
    """)

    # Step 5: persist
    con.execute("DROP TABLE IF EXISTS earnings_dates")
    con.execute("""
        CREATE TABLE earnings_dates AS
        SELECT ticker, earnings_date, crush_date, prev_iv, post_iv, iv_change_pct
        FROM deduped_earnings
        ORDER BY ticker, earnings_date
    """)

    # Summary
    total = con.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0]
    tickers = con.execute("SELECT COUNT(DISTINCT ticker) FROM earnings_dates").fetchone()[0]
    sample = con.execute("""
        SELECT ticker, earnings_date, iv_change_pct
        FROM earnings_dates
        WHERE ticker IN ('AAPL','XOM','BAC','WFC')
        ORDER BY ticker, earnings_date
        LIMIT 16
    """).fetchall()

    print(f"\nEarnings calendar built:")
    print(f"  Events  : {total:,}")
    print(f"  Tickers : {tickers:,}")
    print(f"\nSample (AAPL, XOM, BAC, WFC):")
    print(f"  {'Ticker':<6} {'Earnings Date':<16} {'IV Drop'}")
    print("  " + "-" * 36)
    for row in sample:
        print(f"  {row[0]:<6} {str(row[1]):<16} {row[2]:+.1f}%")


def has_earnings_near(con, ticker: str, trade_date: str, buffer_days: int = 7) -> bool:
    """Check if a ticker has an earnings event within buffer_days of trade_date."""
    result = con.execute("""
        SELECT 1 FROM earnings_dates
        WHERE ticker = ?
          AND ABS(DATEDIFF('day', earnings_date, ?::DATE)) <= ?
        LIMIT 1
    """, [ticker, trade_date, buffer_days]).fetchone()
    return result is not None


if __name__ == '__main__':
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}. Run ingest_orats.py first.")
    else:
        con = duckdb.connect(str(DB_PATH))
        build(con)
        con.close()
        print("\nDone. earnings_dates table ready in options.duckdb.")
