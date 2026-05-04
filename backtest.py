"""
Wheel Strategy Backtest (2020-2025)
------------------------------------
Simulates monthly cash-secured puts → covered calls (the wheel) on each ticker
using BSM pricing with estimated IV = HV * IV_PREMIUM_FACTOR.

Run:  python backtest.py
"""

import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Optional

from config import (
    WATCHLIST, STARTING_CAPITAL, RISK_FREE_RATE,
    IV_PREMIUM_FACTOR, TARGET_DELTA, COMMISSION_PER_CONTRACT,
    HV_LOOKBACK_DAYS, PROFIT_TAKE_PCT, CASH_BUFFER_PCT, MAX_POSITION_PCT,
)
from utils.bsm import price as bsm_price, strike_for_delta
from utils.data_fetcher import get_prices, historical_volatility, iv_rank, next_expiry

BACKTEST_START = '2020-01-01'
BACKTEST_END   = '2025-12-31'

# Use 4 core positions; allocate ~22K each, keep ~12K cash buffer
CORE_TICKERS = ['AAPL', 'XOM', 'BAC', 'WFC']
CAPITAL_PER_TICKER = 22_000   # per position slot
DELTA_TARGET = 0.30           # slightly more aggressive delta for meaningful premium


@dataclass
class Trade:
    ticker: str
    option_type: str          # 'put' or 'call'
    entry_date: date
    expiry: date
    stock_price_at_entry: float
    strike: float
    iv_at_entry: float
    premium: float            # per share
    shares: int = 100
    assigned: bool = False
    close_reason: str = ''    # 'expired', 'assigned', 'profit_take'
    pnl: float = 0.0


@dataclass
class TickerState:
    ticker: str
    cash: float
    shares: int = 0
    cost_basis: float = 0.0
    trades: list = field(default_factory=list)
    monthly_returns: list = field(default_factory=list)


def monthly_entry_dates(start: str, end: str):
    """Yield the first business-ish day of each month in range."""
    current = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while current <= end_ts:
        yield current.date()
        # Advance to first day of next month
        if current.month == 12:
            current = pd.Timestamp(year=current.year + 1, month=1, day=1)
        else:
            current = pd.Timestamp(year=current.year, month=current.month + 1, day=1)


def get_price_on_or_after(prices: pd.Series, target: date) -> Optional[float]:
    """Return the closing price on target date or the next available trading day."""
    ts = pd.Timestamp(target)
    future = prices[prices.index >= ts]
    if future.empty:
        return None
    return float(future.iloc[0])


def simulate_ticker(ticker: str, prices: pd.Series, hv_series: pd.Series) -> TickerState:
    state = TickerState(ticker=ticker, cash=CAPITAL_PER_TICKER)
    prev_portfolio_value = CAPITAL_PER_TICKER

    for entry_date in monthly_entry_dates(BACKTEST_START, BACKTEST_END):
        S = get_price_on_or_after(prices, entry_date)
        if S is None:
            break

        ts_entry = pd.Timestamp(entry_date)
        if ts_entry not in hv_series.index:
            closest = hv_series.index[hv_series.index.searchsorted(ts_entry)]
            ts_entry = closest
        hv = hv_series.get(ts_entry, np.nan)
        if np.isnan(hv) or hv <= 0:
            continue

        iv = hv * IV_PREMIUM_FACTOR
        expiry = next_expiry(entry_date, min_dte=28)
        T = (expiry - entry_date).days / 365.0

        # ── Covered Call (if holding shares) ──────────────────────────────
        if state.shares >= 100:
            n_contracts = state.shares // 100
            K = strike_for_delta(S, T, RISK_FREE_RATE, iv, DELTA_TARGET, 'call')
            premium = bsm_price(S, K, T, RISK_FREE_RATE, iv, 'call')
            premium_collected = premium * 100 * n_contracts
            commission = COMMISSION_PER_CONTRACT * n_contracts
            net_premium = premium_collected - commission
            state.cash += net_premium

            expiry_price = get_price_on_or_after(prices, expiry)
            if expiry_price is None:
                expiry_price = S

            if expiry_price >= K:
                # Called away — sell shares at strike
                proceeds = K * 100 * n_contracts
                state.cash += proceeds
                pnl = net_premium + (K - state.cost_basis) * 100 * n_contracts
                state.shares = 0
                state.cost_basis = 0.0
                reason = 'assigned'
            else:
                pnl = net_premium
                reason = 'expired'

            trade = Trade(
                ticker=ticker, option_type='call',
                entry_date=entry_date, expiry=expiry,
                stock_price_at_entry=S, strike=K, iv_at_entry=iv,
                premium=premium, assigned=(reason == 'assigned'),
                close_reason=reason, pnl=pnl,
            )
            state.trades.append(trade)

        # ── Cash-Secured Put (if in cash) ──────────────────────────────────
        else:
            K = strike_for_delta(S, T, RISK_FREE_RATE, iv, DELTA_TARGET, 'put')
            collateral_per_contract = K * 100

            # Sell as many contracts as capital allows (keep 10% buffer)
            usable_cash = state.cash * 0.90
            if usable_cash < collateral_per_contract:
                state.monthly_returns.append(0.0)
                continue
            n_contracts = max(1, int(usable_cash // collateral_per_contract))

            premium = bsm_price(S, K, T, RISK_FREE_RATE, iv, 'put')
            premium_collected = premium * 100 * n_contracts
            commission = COMMISSION_PER_CONTRACT * n_contracts
            net_premium = premium_collected - commission
            state.cash += net_premium

            expiry_price = get_price_on_or_after(prices, expiry)
            if expiry_price is None:
                expiry_price = S

            if expiry_price <= K:
                # Assigned — buy shares at strike
                total_collateral = collateral_per_contract * n_contracts
                state.cash -= total_collateral
                state.shares = 100 * n_contracts
                state.cost_basis = K
                pnl = net_premium
                reason = 'assigned'
            else:
                pnl = net_premium
                reason = 'expired'

            trade = Trade(
                ticker=ticker, option_type='put',
                entry_date=entry_date, expiry=expiry,
                stock_price_at_entry=S, strike=K, iv_at_entry=iv,
                premium=premium, assigned=(reason == 'assigned'),
                close_reason=reason, pnl=pnl,
            )
            state.trades.append(trade)

        # Mark-to-market portfolio value for this month
        share_value = state.shares * (get_price_on_or_after(prices, expiry) or S)
        portfolio_value = state.cash + share_value
        monthly_ret = (portfolio_value - prev_portfolio_value) / prev_portfolio_value
        state.monthly_returns.append(monthly_ret)
        prev_portfolio_value = portfolio_value

    return state


def run_backtest():
    print("=" * 60)
    print("WHEEL STRATEGY BACKTEST  |  2020–2025")
    print(f"Capital per ticker: ${CAPITAL_PER_TICKER:,.0f}  |  Tickers: {', '.join(WATCHLIST)}")
    print("=" * 60)

    all_states = {}
    all_monthly = {}

    for ticker in CORE_TICKERS:
        print(f"\n  Fetching {ticker}...", end=" ", flush=True)
        try:
            prices = get_prices(ticker, BACKTEST_START, '2026-01-01')
            hv = historical_volatility(prices)
            state = simulate_ticker(ticker, prices, hv)
            all_states[ticker] = state
            all_monthly[ticker] = state.monthly_returns
            print(f"done  ({len(state.trades)} trades)")
        except Exception as e:
            print(f"ERROR: {e}")

    # ── Per-ticker summary ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PER-TICKER RESULTS")
    print("=" * 60)
    print(f"{'Ticker':<8} {'Total Return':>13} {'Ann. Return':>12} {'Avg Mo%':>9} {'Win Rate':>10} {'Assignments':>13} {'Final $':>11}")
    print("-" * 78)

    portfolio_monthly = None

    for ticker, state in all_states.items():
        rets = np.array(state.monthly_returns)
        if len(rets) == 0:
            continue

        total_ret = np.prod(1 + rets) - 1
        n_months = len(rets)
        ann_ret = (1 + total_ret) ** (12 / n_months) - 1
        avg_mo = np.mean(rets) * 100
        win_rate = np.mean(np.array(rets) >= 0) * 100
        assignments = sum(1 for t in state.trades if t.assigned)

        share_price_final = get_prices(ticker, '2025-12-01', '2026-01-15')
        final_price = float(share_price_final.iloc[-1]) if not share_price_final.empty else 0
        final_val = state.cash + state.shares * final_price

        print(f"{ticker:<8} {total_ret*100:>12.1f}% {ann_ret*100:>11.1f}% {avg_mo:>8.2f}% {win_rate:>9.1f}% {assignments:>13d} ${final_val:>10,.0f}")

        # Accumulate portfolio monthly returns (equal weight)
        r_series = pd.Series(rets)
        portfolio_monthly = r_series if portfolio_monthly is None else portfolio_monthly.add(r_series, fill_value=0)

    # ── Portfolio-level summary ───────────────────────────────────────────
    if portfolio_monthly is not None:
        port = portfolio_monthly / len(all_states)
        total = np.prod(1 + port.values) - 1
        n = len(port)
        ann = (1 + total) ** (12 / n) - 1
        avg = port.mean() * 100
        wins = (port >= 0).mean() * 100
        vol = port.std() * np.sqrt(12) * 100
        sharpe = (ann / (port.std() * np.sqrt(12))) if port.std() > 0 else 0
        cash_buffer = STARTING_CAPITAL - len(CORE_TICKERS) * CAPITAL_PER_TICKER
        final_val = cash_buffer + sum(
            s.cash + s.shares * float(get_prices(t, '2025-12-01', '2026-01-15').iloc[-1]
                                       if not get_prices(t, '2025-12-01', '2026-01-15').empty else 0)
            for t, s in all_states.items()
        )

        print("-" * 78)
        print(f"\n{'PORTFOLIO':8} {total*100:>12.1f}% {ann*100:>11.1f}% {avg:>8.2f}% {wins:>9.1f}%")
        print(f"\n  Ann. Volatility   : {vol:.1f}%")
        print(f"  Sharpe Ratio      : {sharpe:.2f}")
        print(f"  Avg monthly return: {avg:.2f}%  (target: 1.00%)")
        print(f"  Months >= 1%      : {(port >= 0.01).sum()}/{n} ({(port>=0.01).mean()*100:.0f}%)")
        print(f"  Max drawdown month: {port.min()*100:.2f}%")
        print(f"  Final Portfolio   : ${final_val:,.0f}  (started ${STARTING_CAPITAL:,.0f})")

    # ── Benchmark: SPY buy-and-hold ───────────────────────────────────────
    print("\n" + "-" * 40)
    try:
        spy = get_prices('SPY', BACKTEST_START, '2026-01-01')
        spy_ret = (spy.iloc[-1] / spy.iloc[0]) - 1
        spy_ann = (1 + spy_ret) ** (12 / 72) - 1  # ~72 months
        spy_final = STARTING_CAPITAL * (1 + spy_ret)
        print(f"  SPY buy-and-hold: {spy_ret*100:.1f}% total  |  {spy_ann*100:.1f}% annual  |  ${spy_final:,.0f}")
    except Exception:
        pass

    # ── Equity curve chart ────────────────────────────────────────────────
    if portfolio_monthly is not None:
        _plot_equity_curve(port, all_states)

    print("\n  Chart saved to backtest_results.png")
    print("=" * 60)


def _plot_equity_curve(port_returns: pd.Series, all_states: dict):
    fig, axes = plt.subplots(2, 1, figsize=(12, 9))

    # Cumulative portfolio equity
    cum = (1 + port_returns).cumprod() * (STARTING_CAPITAL / len(all_states))
    portfolio_cum = (1 + port_returns).cumprod() * STARTING_CAPITAL

    # SPY benchmark
    try:
        spy = get_prices('SPY', BACKTEST_START, '2026-01-01')
        spy_monthly = spy.resample('ME').last().pct_change().dropna()
        n = min(len(portfolio_cum), len(spy_monthly))
        spy_cum = (1 + spy_monthly.iloc[:n].values).cumprod() * STARTING_CAPITAL
        axes[0].plot(range(n), spy_cum, label='SPY Buy & Hold', color='gray', linestyle='--', alpha=0.7)
    except Exception:
        n = len(portfolio_cum)

    axes[0].plot(range(len(portfolio_cum)), portfolio_cum.values, label='Wheel Strategy', color='steelblue', linewidth=2)
    axes[0].axhline(STARTING_CAPITAL, color='black', linestyle=':', alpha=0.5)
    axes[0].set_title('Wheel Strategy vs SPY Buy & Hold  (2020–2025)', fontsize=13)
    axes[0].set_ylabel('Portfolio Value ($)')
    axes[0].legend()
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    axes[0].grid(alpha=0.3)

    # Monthly returns bar chart
    colors = ['green' if r >= 0 else 'red' for r in port_returns.values]
    axes[1].bar(range(len(port_returns)), port_returns.values * 100, color=colors, alpha=0.7)
    axes[1].axhline(1.0, color='orange', linestyle='--', label='1% target', linewidth=1.5)
    axes[1].axhline(0, color='black', linewidth=0.8)
    axes[1].set_title('Monthly Returns (%)', fontsize=12)
    axes[1].set_ylabel('Return (%)')
    axes[1].legend()
    axes[1].grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig('backtest_results.png', dpi=150, bbox_inches='tight')
    plt.close()


if __name__ == '__main__':
    run_backtest()
