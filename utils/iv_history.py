"""
Local IV history store — accumulates daily ATM put implied-volatility readings
so we can compute a proper 52-week IV Rank without a paid real-time feed.

Sources (priority order):
  1. yfinance options chain: average IV of near-ATM puts in the 21-45 DTE expiry
  2. ORATS bootstrap: pre-populate from options.duckdb for tickers in our watchlist

Storage: data/iv_history.csv  (date, ticker, iv, source)
After 252 trading days of yfinance readings the IVR is fully self-contained.
ORATS gives immediate accurate IVR from day one for any ticker covered there.

Usage:
    from utils.iv_history import update, get_ivr, bootstrap_from_orats

    update(WATCHLIST)                  # call once at screener startup
    ivr = get_ivr('AAPL')              # returns 0-100 or None
    bootstrap_from_orats(WATCHLIST)    # one-time seed from ORATS DB
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date
from pathlib import Path

IV_HISTORY_PATH = Path(__file__).parent.parent / 'data' / 'iv_history.csv'
ORATS_DB_PATH   = Path(__file__).parent.parent / 'data' / 'options.duckdb'

_cache: pd.DataFrame | None = None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load() -> pd.DataFrame:
    global _cache
    if _cache is not None:
        return _cache
    if IV_HISTORY_PATH.exists():
        df = pd.read_csv(IV_HISTORY_PATH, parse_dates=['date'])
    else:
        df = pd.DataFrame({'date': pd.Series(dtype='datetime64[ns]'),
                           'ticker': pd.Series(dtype='str'),
                           'iv': pd.Series(dtype='float64'),
                           'source': pd.Series(dtype='str')})
    _cache = df
    return df


def _save(df: pd.DataFrame):
    global _cache
    df = df.sort_values(['ticker', 'date']).reset_index(drop=True)
    df.to_csv(IV_HISTORY_PATH, index=False)
    _cache = df


def _fetch_atm_iv(ticker: str, today: date) -> float | None:
    """
    Fetch ATM put implied volatility from yfinance for the expiry
    closest to 30 DTE (must be 14-50 DTE).
    Returns the mean IV of near-ATM puts, or None on failure.
    """
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None

        # Pick the expiry closest to 30 DTE in the 14-50 day window
        target_exp = None
        best_gap = 999
        for exp_str in exps:
            exp = date.fromisoformat(exp_str)
            dte = (exp - today).days
            if 14 <= dte <= 50:
                gap = abs(dte - 30)
                if gap < best_gap:
                    best_gap = gap
                    target_exp = (exp_str, dte)

        if target_exp is None:
            return None

        exp_str, dte = target_exp
        chain = t.option_chain(exp_str)
        puts = chain.puts
        if puts.empty:
            return None

        # Current price
        S = float(t.fast_info.last_price)
        if S <= 0:
            return None

        # Near-ATM puts: strike within 10% of spot
        ntm = puts[(puts['strike'] >= S * 0.90) & (puts['strike'] <= S * 1.10)]
        if ntm.empty:
            ntm = puts  # fallback: use all puts

        iv_vals = ntm['impliedVolatility'].dropna()
        iv_vals = iv_vals[(iv_vals > 0.01) & (iv_vals < 5.0)]
        if iv_vals.empty:
            return None

        return float(iv_vals.mean())

    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def update(tickers: list[str], verbose: bool = False) -> dict[str, float]:
    """
    Fetch today's ATM put IV for each ticker and overwrite today's stored value.
    Called multiple times per day — the latest reading always wins.
    Returns {ticker: iv} for all tickers with a reading today.
    """
    today = date.today()
    df = _load()

    if verbose:
        print(f"  Fetching IV for {len(tickers)} tickers...", end='', flush=True)

    fetched: dict[str, float] = {}
    for ticker in tickers:
        iv = _fetch_atm_iv(ticker, today)
        if iv is not None:
            fetched[ticker] = iv

    if fetched:
        # Drop any existing rows for today's tickers and replace with fresh values
        mask = (df['date'].dt.date == today) & (df['ticker'].isin(fetched.keys()))
        df = df[~mask].copy()
        new_rows = pd.DataFrame([{
            'date':   pd.Timestamp(today),
            'ticker': tkr,
            'iv':     round(iv, 6),
            'source': 'yfinance',
        } for tkr, iv in fetched.items()])
        df = pd.concat([df, new_rows], ignore_index=True)
        _save(df)

    if verbose:
        print(f" done ({len(fetched)}/{len(tickers)} tickers with IV today)")

    return fetched


def bootstrap_from_orats(tickers: list[str] | None = None, verbose: bool = True):
    """
    Pre-populate iv_history from ORATS options.duckdb using daily average
    smoothSmvVol per ticker.  Safe to re-run — only inserts missing dates.
    """
    if not ORATS_DB_PATH.exists():
        if verbose:
            print("  ORATS DB not found — skipping bootstrap")
        return

    import duckdb
    df = _load()

    existing_keys: set[tuple] = set()
    if not df.empty:
        existing_keys = set(zip(
            df['date'].dt.date.astype(str).tolist(),
            df['ticker'].tolist(),
        ))

    try:
        con = duckdb.connect(str(ORATS_DB_PATH), read_only=True)
    except Exception as e:
        if verbose:
            print(f"  ORATS DB locked (ingest may be running) — skipping bootstrap: {e}")
        return

    ticker_clause = ""
    if tickers:
        quoted = "','".join(tickers)
        ticker_clause = f"AND ticker IN ('{quoted}')"

    orats_df = con.execute(f"""
        SELECT trade_date AS date, ticker, AVG(smoothSmvVol) AS iv
        FROM options
        WHERE smoothSmvVol > 0.01 AND smoothSmvVol < 5.0
          {ticker_clause}
        GROUP BY trade_date, ticker
        ORDER BY ticker, trade_date
    """).df()
    con.close()

    if orats_df.empty:
        if verbose:
            print("  No ORATS data matched — skipping bootstrap")
        return

    orats_df['source'] = 'orats'
    orats_df['date'] = pd.to_datetime(orats_df['date'])

    # Drop already-stored (date, ticker) pairs
    orats_df['_key'] = list(zip(
        orats_df['date'].dt.date.astype(str).tolist(),
        orats_df['ticker'].tolist(),
    ))
    orats_df = orats_df[~orats_df['_key'].isin(existing_keys)].drop(columns='_key')

    if orats_df.empty:
        if verbose:
            print("  IV history already up to date with ORATS data")
        return

    df = pd.concat([df, orats_df], ignore_index=True)
    _save(df)

    if verbose:
        n_tickers = orats_df['ticker'].nunique()
        n_rows    = len(orats_df)
        print(f"  Bootstrapped {n_rows:,} IV readings for {n_tickers} tickers from ORATS")


def get_ivr(ticker: str, as_of: date | None = None, min_days: int = 126) -> float | None:
    """
    Return the 52-week IV Rank (0–100) for ticker as of as_of date.
    Uses the last 252 available readings (trading days).
    Returns None if fewer than min_days of history exist.
    """
    if as_of is None:
        as_of = date.today()

    df = _load()
    if df.empty:
        return None

    ticker_df = df[df['ticker'] == ticker].copy()
    if ticker_df.empty:
        return None

    ticker_df = ticker_df.sort_values('date')
    ticker_df = ticker_df[ticker_df['date'].dt.date <= as_of]

    recent = ticker_df.tail(252)
    if len(recent) < min_days:
        return None

    current_iv = float(recent['iv'].iloc[-1])
    iv_min     = float(recent['iv'].min())
    iv_max     = float(recent['iv'].max())

    if iv_max <= iv_min:
        return None

    return round((current_iv - iv_min) / (iv_max - iv_min) * 100, 1)


def history_summary() -> pd.DataFrame:
    """Return per-ticker coverage stats: first date, last date, row count."""
    df = _load()
    if df.empty:
        return pd.DataFrame()
    return (df.groupby('ticker')
              .agg(first_date=('date', 'min'),
                   last_date=('date', 'max'),
                   n_days=('date', 'count'))
              .reset_index()
              .sort_values('ticker'))
