"""yfinance data helpers."""
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from config import HV_LOOKBACK_DAYS


def get_prices(ticker: str, start: str, end: str) -> pd.Series:
    """Return daily adjusted close prices."""
    df = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"No price data for {ticker}")
    close = df['Close']
    if isinstance(close, pd.DataFrame):
        close = close.squeeze()
    return close.dropna()


def historical_volatility(prices: pd.Series, window: int = HV_LOOKBACK_DAYS) -> pd.Series:
    """Annualised HV using log returns, rolling window."""
    log_returns = np.log(prices / prices.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(252)


def iv_rank(hv_series: pd.Series, lookback: int = 252) -> pd.Series:
    """IV Rank: where current HV sits in its 52-week range (0–100)."""
    rolling_min = hv_series.rolling(lookback).min()
    rolling_max = hv_series.rolling(lookback).max()
    rank = (hv_series - rolling_min) / (rolling_max - rolling_min) * 100
    return rank


def next_expiry(ref_date: date, min_dte: int = 21) -> date:
    """Return the nearest third-Friday expiry at least min_dte days out."""
    d = ref_date + timedelta(days=min_dte)
    # Walk forward to find a Friday that is the 3rd Friday of its month
    for _ in range(60):
        if d.weekday() == 4:  # Friday
            # Count Fridays in this month up to d
            fridays = sum(
                1
                for day in range(1, d.day + 1)
                if date(d.year, d.month, day).weekday() == 4
            )
            if fridays == 3:
                return d
        d += timedelta(days=1)
    raise ValueError("Could not find expiry date")


def get_current_chain(ticker: str):
    """Return the nearest available options expiry chain as (calls_df, puts_df, expiry_str)."""
    t = yf.Ticker(ticker)
    expirations = t.options
    if not expirations:
        return None, None, None
    expiry = expirations[0]
    chain = t.option_chain(expiry)
    return chain.calls, chain.puts, expiry
