"""
Daily options screener — finds the best cash-secured put candidates.

Key improvements over v1:
  - IVR computed from accumulated yfinance IV history (real implied vol, not HV proxy)
  - Actual option bids fetched from the live chain — no more BSM estimates
  - Delta range 0.25–0.35 matches the backtest exactly
  - Broader universe (80+ tickers) ensures IVR≥40 slots are always available
"""

import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import date, timedelta
from typing import List, Dict

from config import (
    WATCHLIST, RISK_FREE_RATE, IV_PREMIUM_FACTOR,
    DELTA_LOW, DELTA_HIGH, CC_DELTA_LOW, CC_DELTA_HIGH,
    DTE_MIN, DTE_MAX, HV_LOOKBACK_DAYS, MIN_IVR,
    MIN_STOCK_PRICE, MAX_STOCK_PRICE,
    MIN_OPTIONS_OI, MIN_OPTIONS_VOLUME, MAX_SPREAD_PCT,
    EARNINGS_BUFFER_DAYS, TICKER_SECTORS,
)
from utils import bsm
from utils.data_fetcher import get_prices, historical_volatility, next_expiry
from utils.iv_history import update as update_iv_history, get_ivr

BLOCKLIST = {
    'UVXY', 'SVXY', 'VXX', 'VIXY',
    'SQQQ', 'TQQQ', 'SPXU', 'SPXL',
    'LABD', 'LABU', 'SOXS', 'SOXL',
    'YANG', 'YINN', 'TZA', 'TNA', 'FAS', 'FAZ',
}


# ── Options chain helpers ──────────────────────────────────────────────────────

def _days_to_earnings(ticker: str) -> int | None:
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None or cal.empty:
            return None
        earnings_dates = cal.loc['Earnings Date'] if 'Earnings Date' in cal.index else None
        if earnings_dates is None:
            return None
        next_earn = pd.to_datetime(earnings_dates.iloc[0])
        return (next_earn.date() - date.today()).days
    except Exception:
        return None


def _find_target_put(ticker: str, S: float, today: date) -> dict | None:
    """
    Search the live options chain for a put with delta in [DELTA_LOW, DELTA_HIGH]
    and DTE in [DTE_MIN, DTE_MAX].  Returns the contract closest to delta 0.30
    with a non-zero bid, or None.
    """
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None

        # Collect all expiries in the DTE window, sorted by closeness to 30 DTE
        valid_exps = []
        for exp_str in exps:
            exp = date.fromisoformat(exp_str)
            dte = (exp - today).days
            if DTE_MIN <= dte <= DTE_MAX:
                valid_exps.append((abs(dte - 30), exp_str, exp, dte))
        valid_exps.sort()

        if not valid_exps:
            return None

        # Try each expiry in order until one yields a valid candidate
        # (weekly expiries often have no liquid contracts despite being in the DTE window)
        mid_delta = (DELTA_LOW + DELTA_HIGH) / 2
        candidates = []
        exp_str = exp_date = dte = T = None

        for _, exp_str, exp_date, dte in valid_exps:
            T = dte / 365.0
            chain = t.option_chain(exp_str)
            puts = chain.puts
            if puts.empty:
                continue

            candidates = []
            for _, row in puts.iterrows():
                iv_est = float(row.get('impliedVolatility', 0) or 0)
                bid    = float(row.get('bid', 0) or 0)
                ask    = float(row.get('ask', 0) or 0)
                oi     = int(row.get('openInterest', 0) or 0)

                if iv_est <= 0 or bid <= 0:
                    continue
                if oi < MIN_OPTIONS_OI:
                    continue

                # Spread filter
                mid = (bid + ask) / 2 if ask > 0 else bid
                if mid > 0 and (ask - bid) / mid > MAX_SPREAD_PCT:
                    continue

                d_abs = abs(bsm.delta(S, float(row['strike']), T, RISK_FREE_RATE, iv_est, 'put'))
                if DELTA_LOW <= d_abs <= DELTA_HIGH:
                    candidates.append({
                        'strike':     float(row['strike']),
                        'bid':        bid,
                        'ask':        ask,
                        'delta':      d_abs,
                        'iv':         iv_est,
                        'expiry':     exp_date,
                        'expiry_str': exp_date.strftime('%b %d'),
                        'dte':        dte,
                        'oi':         oi,
                    })

            if candidates:
                break  # found valid candidates on this expiry — stop trying others

        if not candidates:
            return None

        # Closest to 0.30 delta
        candidates.sort(key=lambda x: abs(x['delta'] - mid_delta))
        return candidates[0]

    except Exception:
        return None


def _find_target_call(ticker: str, S: float, cost_basis: float, today: date) -> dict | None:
    """Find a covered call for a share position: delta 0.25-0.35, strike >= cost_basis."""
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None

        target_exp = None
        best_gap = 999
        for exp_str in exps:
            exp = date.fromisoformat(exp_str)
            dte = (exp - today).days
            if DTE_MIN <= dte <= DTE_MAX:
                gap = abs(dte - 30)
                if gap < best_gap:
                    best_gap = gap
                    target_exp = (exp_str, exp, dte)

        if target_exp is None:
            return None

        exp_str, exp_date, dte = target_exp
        T = dte / 365.0

        chain = t.option_chain(exp_str)
        calls = chain.calls
        if calls.empty:
            return None

        mid_delta = (CC_DELTA_LOW + CC_DELTA_HIGH) / 2
        candidates = []

        for _, row in calls.iterrows():
            strike = float(row['strike'])
            if strike < cost_basis:
                continue
            iv_est = float(row.get('impliedVolatility', 0) or 0)
            bid    = float(row.get('bid', 0) or 0)
            if iv_est <= 0 or bid <= 0:
                continue

            d_abs = abs(bsm.delta(S, strike, T, RISK_FREE_RATE, iv_est, 'call'))
            if CC_DELTA_LOW <= d_abs <= CC_DELTA_HIGH:
                candidates.append({
                    'strike':     strike,
                    'bid':        bid,
                    'delta':      d_abs,
                    'expiry':     exp_str,            # ISO string e.g. '2026-07-17'
                    'expiry_str': exp_date.strftime('%b %d'),
                    'dte':        dte,
                })

        if not candidates:
            return None

        candidates.sort(key=lambda x: abs(x['delta'] - mid_delta))
        return candidates[0]

    except Exception:
        return None


# ── Main screener ──────────────────────────────────────────────────────────────

def screen(watchlist: List[str] = WATCHLIST, top_n: int = 8,
           min_ivr: float = MIN_IVR) -> List[Dict]:
    """
    Screen the watchlist for put-selling candidates.
    1. Updates iv_history with today's readings for all tickers.
    2. For each ticker, checks IVR, earnings proximity, liquidity.
    3. Finds actual chain bid for the target delta put.
    4. Returns top_n ranked by IVR × put yield score.
    """
    today = date.today()

    # Step 1: update IV history for everything in the watchlist (one batch)
    iv_today = update_iv_history([t for t in watchlist if t not in BLOCKLIST],
                                 verbose=True)

    results = []

    for ticker in watchlist:
        if ticker in BLOCKLIST:
            continue

        try:
            # ── IVR gate ──────────────────────────────────────────────────
            ivr = get_ivr(ticker)

            # Fallback: estimate IVR from HV if history is thin (<126 days)
            if ivr is None:
                try:
                    start = (today - timedelta(days=650)).strftime('%Y-%m-%d')
                    prices = get_prices(ticker, start, today.strftime('%Y-%m-%d'))
                    if len(prices) >= HV_LOOKBACK_DAYS + 126:
                        hv = historical_volatility(prices)
                        r_min = hv.rolling(252).min()
                        r_max = hv.rolling(252).max()
                        ivr = float(
                            (hv.iloc[-1] - r_min.iloc[-1])
                            / (r_max.iloc[-1] - r_min.iloc[-1]) * 100
                        ) if r_max.iloc[-1] > r_min.iloc[-1] else None
                except Exception:
                    pass

            if ivr is None or ivr < min_ivr:
                continue

            # ── Stock price ───────────────────────────────────────────────
            t_obj = yf.Ticker(ticker)
            S = float(t_obj.fast_info.last_price)
            if not (MIN_STOCK_PRICE <= S <= MAX_STOCK_PRICE):
                continue

            # ── Earnings proximity ────────────────────────────────────────
            dte_earnings = _days_to_earnings(ticker)
            if dte_earnings is not None and 0 <= dte_earnings <= EARNINGS_BUFFER_DAYS:
                continue

            # ── Find actual put bid for target delta ──────────────────────
            put = _find_target_put(ticker, S, today)
            if put is None:
                continue

            put_yield     = (put['bid'] / put['strike']) * 100
            put_ann_yield = put_yield * (365 / put['dte'])

            # ── IV for display (prefer today's stored value) ───────────────
            iv_pct = round((iv_today.get(ticker, put['iv'])) * 100, 1)

            # ── Score: IVR drives quality, yield drives magnitude ──────────
            score = round(ivr * 0.6 + put_ann_yield * 0.4, 1)

            results.append({
                'ticker':       ticker,
                'sector':       TICKER_SECTORS.get(ticker, 'Other'),
                'price':        round(S, 2),
                'iv_rank':      ivr,
                'iv_pct':       iv_pct,
                'put_strike':   put['strike'],
                'put_bid':      round(put['bid'], 2),
                'put_delta':    round(put['delta'], 2),
                'put_yield_pct': round(put_yield, 2),
                'put_ann_yield': round(put_ann_yield, 1),
                'expiry':       put['expiry'].isoformat(),
                'expiry_str':   put['expiry_str'],
                'dte':          put['dte'],
                'earnings_in':  dte_earnings,
                'score':        score,
            })

        except Exception:
            continue

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:top_n]


if __name__ == '__main__':
    from tabulate import tabulate
    print(f"Running screener ({len(WATCHLIST)} tickers, IVR ≥ {MIN_IVR})...\n")
    candidates = screen()
    if not candidates:
        print("No candidates pass all filters today.")
    else:
        rows = [
            [r['ticker'],
             f"${r['price']}",
             f"{r['iv_rank']:.0f}",
             f"{r['iv_pct']}%",
             f"${r['put_strike']} @ ${r['put_bid']}  Δ{r['put_delta']}  ({r['put_yield_pct']}%)",
             f"{r['put_ann_yield']:.0f}%/yr",
             r['expiry'],
             r['dte'],
             f"{r['earnings_in']}d" if r['earnings_in'] is not None else '—',
             r['score']]
            for r in candidates
        ]
        print(tabulate(rows,
                       headers=['Ticker', 'Price', 'IVR', 'IV', 'Best Put (actual bid)',
                                'Ann Yld', 'Expiry', 'DTE', 'Earn', 'Score'],
                       tablefmt='rounded_outline'))
