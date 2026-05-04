"""
Wheel Strategy Backtest using real ORATS historical options data.
Uses actual bid prices for fills, real Greeks for strike selection,
and the full stock universe (filtered for quality) rather than a fixed watchlist.

Run:  python backtest_orats.py
Requires: data/options.duckdb populated by ingest_orats.py
"""

import warnings
warnings.filterwarnings('ignore')

import duckdb
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from datetime import date
from dataclasses import dataclass, field

DB_PATH = Path(__file__).parent / 'data' / 'options.duckdb'

# ── Fixed parameters (shared across all strategies) ───────────────────────────
STARTING_CAPITAL     = 100_000
TARGET_DELTA_LOW     = 0.25
TARGET_DELTA_HIGH    = 0.35
DTE_MIN              = 21
DTE_MAX              = 45
PROFIT_TAKE_PCT      = 0.50
MAX_POSITIONS        = 5
POSITION_SIZE_PCT    = 0.20
COMMISSION           = 0.65
EARNINGS_BUFFER_DAYS = 7

MIN_STOCK_PRICE  = 20.0
MAX_STOCK_PRICE  = 500.0
MIN_PUT_BID      = 0.30
MIN_OI           = 500

BLOCKLIST = {
    'UVXY','SVXY','VXX','VIXY','SQQQ','TQQQ','SPXU','SPXL',
    'LABD','LABU','SOXS','SOXL','YANG','YINN','TZA','TNA','FAS','FAZ',
}

# ── Strategy variants ─────────────────────────────────────────────────────────
@dataclass
class StrategyConfig:
    name:                  str
    min_ivr:               float        # primary IVR entry filter
    cc_recovery_threshold: float        # 0.0 = write CC immediately after assignment
    cc_delta_low:          float = 0.25 # covered call delta range
    cc_delta_high:         float = 0.35
    min_ivr_fallback:      float = None # relax to this IVR when slots go unfilled
    sell_covered_calls:    bool  = True # False = hold shares after assignment, no CC
    stop_loss_pct:         float = None # cut share position if stock falls this far below cost basis

STRATEGIES = [
    StrategyConfig(
        "Baseline  (IVR≥40, CC immediately, no stop)",
        min_ivr=40, cc_recovery_threshold=0.0,
    ),
    StrategyConfig(
        "Rules-Based Wheel  (put stop @−20%, CC ≥cost after recovery)",
        min_ivr=40, cc_recovery_threshold=1.0,
        sell_covered_calls=True, stop_loss_pct=0.20,
    ),
]


# ── Position / state ──────────────────────────────────────────────────────────
@dataclass
class Position:
    ticker:      str
    option_type: str       # 'put', 'call', 'shares'
    entry_date:  date
    expiry:      date
    stock_price: float
    strike:      float
    premium:     float
    contracts:   int
    shares_held: int   = 0
    cost_basis:  float = 0.0

    @property
    def collateral(self):
        return self.strike * 100 * self.contracts

    @property
    def net_premium(self):
        return self.premium * 100 * self.contracts - COMMISSION * self.contracts * 2


@dataclass
class BacktestState:
    cash:        float = STARTING_CAPITAL
    positions:   list  = field(default_factory=list)
    trades:      list  = field(default_factory=list)
    equity:      list  = field(default_factory=list)
    open_events: list  = field(default_factory=list)
    idle_log:    list  = field(default_factory=list)   # (date, idle_cash, put_col, share_val, idle_slots)


# ── DuckDB helpers ────────────────────────────────────────────────────────────
def get_trading_dates(con) -> list[date]:
    rows = con.execute("""
        SELECT DISTINCT trade_date FROM options
        WHERE trade_date IS NOT NULL
        ORDER BY trade_date
    """).fetchall()
    return [r[0] for r in rows]


def build_ivr_table(con):
    """
    Pre-compute 52-week IV Rank for every (ticker, trade_date).
    Built once; shared across all strategy runs via a temp table.
    """
    print("Pre-computing IV Rank table (one-time ~30s)...", end='', flush=True)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE daily_ivr AS
        WITH daily_avg AS (
            SELECT ticker, trade_date, AVG(smoothSmvVol) AS avg_iv
            FROM options
            WHERE smoothSmvVol > 0 AND smoothSmvVol < 5.0
            GROUP BY ticker, trade_date
        ),
        rolling AS (
            SELECT
                ticker, trade_date, avg_iv,
                MIN(avg_iv) OVER w AS iv_52w_low,
                MAX(avg_iv) OVER w AS iv_52w_high,
                COUNT(*)    OVER w AS days_in_window
            FROM daily_avg
            WINDOW w AS (
                PARTITION BY ticker ORDER BY trade_date
                ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
            )
        )
        SELECT
            ticker,
            trade_date,
            CASE
                WHEN iv_52w_high > iv_52w_low AND days_in_window >= 126
                THEN ROUND((avg_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100, 1)
                ELSE NULL
            END AS ivr
        FROM rolling
    """)
    n = con.execute("SELECT COUNT(*) FROM daily_ivr WHERE ivr IS NOT NULL").fetchone()[0]
    print(f" done ({n:,} ticker-dates with valid IVR)")


def _query_candidates(con, trade_date: date, min_ivr: float) -> pd.DataFrame:
    delta_lo      = 1 - TARGET_DELTA_HIGH
    delta_hi      = 1 - TARGET_DELTA_LOW
    blocklist_sql = "','".join(BLOCKLIST)
    return con.execute(f"""
        SELECT
            o.ticker, o.stkPx, o.strike, o.expirDate,
            CAST(o.expirDate AS DATE) - CAST('{trade_date}' AS DATE) AS dte,
            o.pBidPx, o.pAskPx,
            (1 - o.delta)  AS put_delta_abs,
            o.smoothSmvVol AS iv,
            d.ivr,
            o.pOi
        FROM options o
        JOIN daily_ivr d ON d.ticker = o.ticker AND d.trade_date = o.trade_date
        WHERE o.trade_date = '{trade_date}'
          AND o.stkPx  BETWEEN {MIN_STOCK_PRICE} AND {MAX_STOCK_PRICE}
          AND o.pBidPx >= {MIN_PUT_BID}
          AND o.pOi    >= {MIN_OI}
          AND o.delta  BETWEEN {delta_lo:.4f} AND {delta_hi:.4f}
          AND o.yte    BETWEEN {DTE_MIN/365.0:.4f} AND {DTE_MAX/365.0:.4f}
          AND o.ticker NOT IN ('{blocklist_sql}')
          AND o.smoothSmvVol > 0 AND o.smoothSmvVol < 5.0
          AND d.ivr >= {min_ivr}
        ORDER BY (o.pBidPx / o.strike) DESC
    """).df()


def get_candidates(con, trade_date: date, state: BacktestState,
                   cfg: StrategyConfig, slots: int) -> pd.DataFrame:
    open_tickers = {p.ticker for p in state.positions}

    df = _query_candidates(con, trade_date, cfg.min_ivr)

    # Fallback: if primary yields fewer tickers than open slots, top up with looser IVR
    if cfg.min_ivr_fallback is not None and len(df.drop_duplicates('ticker')) < slots:
        df_fb = _query_candidates(con, trade_date, cfg.min_ivr_fallback)
        df = pd.concat([df, df_fb], ignore_index=True)

    if open_tickers:
        df = df[~df['ticker'].isin(open_tickers)]

    if not df.empty:
        try:
            near_earnings = con.execute(f"""
                SELECT DISTINCT ticker FROM earnings_dates
                WHERE ABS(DATEDIFF('day', earnings_date, '{trade_date}')) <= {EARNINGS_BUFFER_DAYS}
            """).df()['ticker'].tolist()
            df = df[~df['ticker'].isin(near_earnings)]
        except Exception:
            pass

    df = df.sort_values('pBidPx', ascending=False)
    df = df.drop_duplicates(subset=['ticker'], keep='first')
    return df.head(20)


def get_option_price(con, ticker, expiry, strike, option_type, trade_date):
    col = 'pBidPx' if option_type == 'put' else 'cBidPx'
    row = con.execute(f"""
        SELECT {col} FROM options
        WHERE trade_date = '{trade_date}' AND ticker = '{ticker}'
          AND expirDate = '{expiry}' AND ABS(strike - {strike}) < 0.01
        LIMIT 1
    """).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def get_stock_price(con, ticker, trade_date):
    row = con.execute(f"""
        SELECT stkPx FROM options
        WHERE trade_date = '{trade_date}' AND ticker = '{ticker}'
        LIMIT 1
    """).fetchone()
    return float(row[0]) if row else None


def get_stock_price_on_or_before(con, ticker, target_date):
    row = con.execute(f"""
        SELECT stkPx FROM options
        WHERE ticker = '{ticker}' AND trade_date <= '{target_date}'
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    return float(row[0]) if row else None


def get_intraperiod_low(con, ticker: str, start_date: date, end_date: date):
    """Min stock price in the window (start_date, end_date] — used to detect stop crossings."""
    row = con.execute(f"""
        SELECT MIN(stkPx) FROM options
        WHERE ticker = '{ticker}'
          AND trade_date >  '{start_date}'
          AND trade_date <= '{end_date}'
    """).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def first_trading_day_of_month(dates, year, month):
    for d in dates:
        if d.year == year and d.month == month:
            return d
    return None


# ── Core backtest loop ────────────────────────────────────────────────────────
def run_one(con, all_dates: list[date], cfg: StrategyConfig) -> BacktestState:
    """
    Daily simulation: every trading day we check positions, fire stop-losses,
    profit-take, and open new puts whenever a slot is free. This matches how
    the live screener actually operates.
    """
    state           = BacktestState()
    last_idle_month = None

    for trade_date in all_dates:
        # ── Manage existing positions ──────────────────────────────────────
        still_open = []
        for pos in state.positions:
            # Shares: check stop-loss daily; no option expiry logic applies
            if pos.option_type == 'shares':
                if cfg.stop_loss_pct is not None:
                    cur_px     = get_stock_price(con, pos.ticker, trade_date) or pos.stock_price
                    stop_level = pos.cost_basis * (1 - cfg.stop_loss_pct)
                    if cur_px <= stop_level:
                        # Daily checks mean the stock only just crossed stop_level today,
                        # so cur_px ≈ stop_level. Execute at cur_px (actual market price).
                        proceeds = cur_px * pos.shares_held
                        pnl      = proceeds - pos.cost_basis * pos.shares_held
                        state.cash += proceeds
                        state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                             'type': 'stop_loss', 'pnl': pnl,
                                             'strike': pos.cost_basis, 'stock_px': cur_px})
                        continue
                still_open.append(pos)
                continue

            current_price = get_option_price(
                con, pos.ticker, pos.expiry, pos.strike, pos.option_type, trade_date
            )

            if pos.expiry <= trade_date:
                stock_px = (get_stock_price_on_or_before(con, pos.ticker, pos.expiry)
                            or pos.stock_price)

                if pos.option_type == 'put' and stock_px < pos.strike:
                    state.cash += pos.net_premium
                    stop_level = pos.strike * (1 - cfg.stop_loss_pct) if cfg.stop_loss_pct else None

                    if stop_level is not None and stock_px < stop_level:
                        # Stock already past the stop threshold at expiry — a real GTC
                        # stop order would have fired before now. Cap the loss at exactly
                        # stop_loss_pct rather than accepting a deep assignment.
                        proceeds = stop_level * pos.contracts * 100
                        pnl      = pos.net_premium + proceeds - pos.collateral
                        state.cash += proceeds
                        state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                             'type': 'stop_loss', 'pnl': pnl,
                                             'strike': pos.strike, 'stock_px': stop_level})
                    else:
                        state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                             'type': 'put_assigned', 'pnl': pos.net_premium,
                                             'strike': pos.strike, 'stock_px': stock_px})
                        still_open.append(Position(
                            ticker=pos.ticker, option_type='shares',
                            entry_date=trade_date, expiry=pos.expiry,
                            stock_price=stock_px, strike=pos.strike,
                            premium=0, contracts=pos.contracts,
                            shares_held=pos.contracts * 100, cost_basis=pos.strike,
                        ))

                elif pos.option_type == 'call' and stock_px >= pos.strike:
                    pnl = pos.net_premium + (pos.strike - pos.cost_basis) * 100 * pos.contracts
                    state.cash += pos.strike * 100 * pos.contracts + pos.net_premium
                    state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                         'type': 'call_assigned', 'pnl': pnl,
                                         'strike': pos.strike, 'stock_px': stock_px})

                else:
                    if pos.option_type == 'put':
                        state.cash += pos.collateral
                    state.cash += pos.net_premium
                    state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                         'type': f'{pos.option_type}_expired',
                                         'pnl': pos.net_premium,
                                         'strike': pos.strike, 'stock_px': stock_px})
                    if pos.option_type == 'call' and pos.shares_held > 0:
                        still_open.append(Position(
                            ticker=pos.ticker, option_type='shares',
                            entry_date=trade_date, expiry=pos.expiry,
                            stock_price=stock_px, strike=pos.cost_basis,
                            premium=0, contracts=pos.contracts,
                            shares_held=pos.shares_held, cost_basis=pos.cost_basis,
                        ))

            elif (pos.option_type in ('put', 'call') and
                  current_price and current_price <= pos.premium * (1 - PROFIT_TAKE_PCT)):
                close_cost = current_price * 100 * pos.contracts + COMMISSION * pos.contracts
                pnl = pos.net_premium - close_cost
                if pos.option_type == 'put':
                    state.cash += pos.collateral
                state.cash += pos.net_premium - close_cost
                state.trades.append({'date': trade_date, 'ticker': pos.ticker,
                                     'type': f'{pos.option_type}_profit_take', 'pnl': pnl,
                                     'strike': pos.strike, 'stock_px': pos.stock_price})
                if pos.option_type == 'call' and pos.shares_held > 0:
                    still_open.append(Position(
                        ticker=pos.ticker, option_type='shares',
                        entry_date=trade_date, expiry=pos.expiry,
                        stock_price=pos.stock_price, strike=pos.cost_basis,
                        premium=0, contracts=pos.contracts,
                        shares_held=pos.shares_held, cost_basis=pos.cost_basis,
                    ))
            else:
                still_open.append(pos)

        state.positions = still_open

        # ── Sell covered calls on recovered share positions ────────────────
        if cfg.sell_covered_calls:
            new_positions = []
            for pos in state.positions:
                if pos.option_type == 'shares' and pos.shares_held >= 100:
                    if cfg.cc_recovery_threshold > 0:
                        cur_px = get_stock_price(con, pos.ticker, trade_date) or pos.stock_price
                        if cur_px < pos.cost_basis * cfg.cc_recovery_threshold:
                            new_positions.append(pos)
                            continue

                    cc_mid = (cfg.cc_delta_low + cfg.cc_delta_high) / 2
                    row = con.execute(f"""
                        SELECT strike, cBidPx, expirDate, delta
                        FROM options
                        WHERE trade_date = '{trade_date}' AND ticker = '{pos.ticker}'
                          AND cBidPx >= 0.05
                          AND delta BETWEEN {cfg.cc_delta_low:.4f} AND {cfg.cc_delta_high:.4f}
                          AND yte   BETWEEN {DTE_MIN/365.0:.4f}   AND {DTE_MAX/365.0:.4f}
                          AND strike >= {pos.cost_basis:.2f}
                        ORDER BY ABS(delta - {cc_mid:.4f})
                        LIMIT 1
                    """).fetchone()

                    if row:
                        strike, bid, expiry, _ = row
                        if hasattr(expiry, 'date'):
                            expiry = expiry.date()
                        n_calls = pos.shares_held // 100
                        new_positions.append(Position(
                            ticker=pos.ticker, option_type='call',
                            entry_date=trade_date, expiry=expiry,
                            stock_price=pos.stock_price, strike=strike,
                            premium=float(bid), contracts=n_calls,
                            shares_held=pos.shares_held, cost_basis=pos.cost_basis,
                        ))
                        state.open_events.append({
                            'date': trade_date, 'ticker': pos.ticker, 'type': 'call',
                            'strike': strike, 'expiry': expiry, 'contracts': n_calls,
                        })
                    else:
                        new_positions.append(pos)
                else:
                    new_positions.append(pos)
            state.positions = new_positions

        # ── Open new CSPs whenever a slot is free ──────────────────────────
        open_count = sum(1 for p in state.positions if p.option_type in ('put', 'call'))
        slots      = MAX_POSITIONS - open_count

        if slots > 0:
            candidates = get_candidates(con, trade_date, state, cfg, slots)
            for _, row in candidates.iterrows():
                if slots <= 0:
                    break
                portfolio_value  = state.cash + sum(
                    p.collateral for p in state.positions if p.option_type == 'put'
                )
                position_capital = portfolio_value * POSITION_SIZE_PCT
                collateral_per   = row['strike'] * 100
                if collateral_per > state.cash * 0.90:
                    continue
                n = max(1, int(min(position_capital, state.cash * 0.90) // collateral_per))
                pos = Position(
                    ticker=row['ticker'], option_type='put',
                    entry_date=trade_date,
                    expiry=pd.Timestamp(row['expirDate']).date(),
                    stock_price=row['stkPx'], strike=row['strike'],
                    premium=float(row['pBidPx']), contracts=n,
                )
                state.cash -= pos.collateral
                state.positions.append(pos)
                state.open_events.append({
                    'date': trade_date, 'ticker': pos.ticker, 'type': 'put',
                    'strike': pos.strike, 'expiry': pos.expiry, 'contracts': n,
                })
                slots -= 1

        # ── Daily equity snapshot ──────────────────────────────────────────
        put_col     = sum(p.collateral for p in state.positions if p.option_type == 'put')
        share_value = sum(
            (get_stock_price(con, p.ticker, trade_date) or p.stock_price) * p.shares_held
            for p in state.positions if p.shares_held > 0
        )
        portfolio_val = state.cash + put_col + share_value
        state.equity.append((trade_date, portfolio_val))

        # ── Monthly idle capital snapshot ──────────────────────────────────
        cur_month = (trade_date.year, trade_date.month)
        if cur_month != last_idle_month:
            open_opts    = sum(1 for p in state.positions if p.option_type in ('put', 'call'))
            idle_slots   = max(0, MAX_POSITIONS - open_opts)
            position_sz  = portfolio_val * POSITION_SIZE_PCT
            idle_capital = min(idle_slots * position_sz, max(0, state.cash - portfolio_val * 0.05))
            state.idle_log.append((trade_date, idle_capital, put_col, share_value, idle_slots))
            last_idle_month = cur_month

    return state


# ── Stats helper ──────────────────────────────────────────────────────────────
def summarise(state: BacktestState, cfg: StrategyConfig) -> dict:
    eq_daily = pd.DataFrame(state.equity, columns=['date', 'value'])
    eq_daily['date'] = pd.to_datetime(eq_daily['date'])

    # Resample to month-end for return statistics
    eq_mo = (eq_daily.set_index('date')['value']
             .resample('ME').last()
             .reset_index())
    eq_mo['ret'] = eq_mo['value'].pct_change()

    total_ret = (eq_daily['value'].iloc[-1] / STARTING_CAPITAL) - 1
    n_years   = (eq_daily['date'].iloc[-1] - eq_daily['date'].iloc[0]).days / 365.25
    ann_ret   = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
    vol       = eq_mo['ret'].std() * np.sqrt(12)
    sharpe    = ann_ret / vol if vol > 0 else 0
    worst_mo  = eq_mo['ret'].min() * 100
    win_rate  = (eq_mo['ret'] >= 0).mean() * 100

    trades_df = pd.DataFrame(state.trades)
    n_trades  = len(trades_df)

    print(f"\n{'─'*65}")
    print(f"  {cfg.name}")
    print(f"{'─'*65}")
    print(f"  Total return    : {total_ret*100:.1f}%")
    print(f"  Annual return   : {ann_ret*100:.1f}%")
    print(f"  Avg monthly     : {eq_mo['ret'].mean()*100:.2f}%  (target 1.00%)")
    print(f"  Win rate        : {win_rate:.1f}%")
    print(f"  Sharpe ratio    : {sharpe:.2f}")
    print(f"  Worst month     : {worst_mo:.2f}%")
    print(f"  Final value     : ${eq_daily['value'].iloc[-1]:,.0f}")
    print(f"  Total trades    : {n_trades}")
    if not trades_df.empty:
        by_type = trades_df.groupby('type')['pnl'].agg(['count', 'sum'])
        for outcome, r in by_type.iterrows():
            print(f"    {outcome:<22} {int(r['count']):>4}  ${r['sum']:>9,.0f}")

    return {
        'name': cfg.name,
        'equity': eq_daily,   # daily — smooth equity curves in charts
        'equity_mo': eq_mo,   # monthly — for return bars and stats
        'total_ret': total_ret, 'ann_ret': ann_ret,
        'avg_mo': eq_mo['ret'].mean() * 100, 'win_rate': win_rate,
        'sharpe': sharpe, 'worst_mo': worst_mo,
        'final_value': eq_daily['value'].iloc[-1], 'n_trades': n_trades,
    }


# ── Yearly P&L and monthly symbol reports ────────────────────────────────────

def save_yearly_pnl_chart(state: BacktestState):
    trades_df = pd.DataFrame(state.trades)
    equity_df = pd.DataFrame(state.equity, columns=['date', 'value'])
    if trades_df.empty:
        return

    trades_df['year'] = pd.to_datetime(trades_df['date']).dt.year
    equity_df['date'] = pd.to_datetime(equity_df['date'])
    # Use year-end value for each year
    equity_df = equity_df.set_index('date').resample('YE').last().reset_index()
    equity_df['year'] = equity_df['date'].dt.year

    yearly = trades_df.groupby('year').agg(
        realized_pnl=('pnl', 'sum'),
        n_trades=('pnl', 'count'),
        n_wins=('pnl', lambda x: (x > 0).sum()),
    ).reset_index()

    year_end   = equity_df.groupby('year')['value'].last()
    year_start = {y: STARTING_CAPITAL if i == 0 else year_end.iloc[i - 1]
                  for i, y in enumerate(year_end.index)}

    rows = []
    for _, r in yearly.iterrows():
        yr       = int(r['year'])
        pnl      = r['realized_pnl']
        n        = int(r['n_trades'])
        win_pct  = r['n_wins'] / n * 100 if n else 0
        end_val  = year_end.get(yr, STARTING_CAPITAL)
        port_ret = (end_val - year_start.get(yr, STARTING_CAPITAL)) / year_start.get(yr, STARTING_CAPITAL) * 100
        rows.append([str(yr),
                     f"{'+'if pnl>=0 else ''}${pnl:,.0f}",
                     f"{port_ret:+.1f}%",
                     f"${end_val:,.0f}",
                     str(n),
                     f"{win_pct:.0f}%"])

    total_pnl = trades_df['pnl'].sum()
    final_val = equity_df['value'].iloc[-1]
    total_ret = (final_val - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    rows.append(['TOTAL',
                 f"{'+'if total_pnl>=0 else ''}${total_pnl:,.0f}",
                 f"{total_ret:+.1f}%",
                 f"${final_val:,.0f}",
                 str(len(trades_df)),
                 ''])

    cols = ['Year', 'Realized P&L', 'Portfolio Return', 'Year-End Value', 'Trades', 'Win %']

    fig, (ax_tbl, ax_bar) = plt.subplots(1, 2, figsize=(13, 3.2),
                                          gridspec_kw={'width_ratios': [2.2, 1]})
    fig.suptitle('Yearly Performance Summary  (Baseline strategy, $100K capital)',
                 fontsize=12, fontweight='bold', y=1.02)

    ax_tbl.axis('off')
    tbl = ax_tbl.table(cellText=rows, colLabels=cols, loc='center', cellLoc='center')
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1, 1.9)

    # Colour P&L column green/red
    for i, r in enumerate(rows):
        cell = tbl[(i + 1, 1)]
        val  = yearly['realized_pnl'].iloc[i] if i < len(yearly) else total_pnl
        cell.set_facecolor('#d5f5e3' if val >= 0 else '#fadbd8')
        # Bold the TOTAL row
        if i == len(rows) - 1:
            for j in range(len(cols)):
                tbl[(i + 1, j)].set_text_props(fontweight='bold')
                tbl[(i + 1, j)].set_facecolor('#eaecee')
    # Header styling
    for j in range(len(cols)):
        tbl[(0, j)].set_facecolor('#2c3e50')
        tbl[(0, j)].set_text_props(color='white', fontweight='bold')

    # Bar chart of realized P&L per year
    years    = [int(r[0]) for r in rows[:-1]]
    pnl_vals = [yearly['realized_pnl'].iloc[i] for i in range(len(yearly))]
    bar_cols = ['#2ecc71' if v >= 0 else '#e74c3c' for v in pnl_vals]
    bars = ax_bar.bar(years, pnl_vals, color=bar_cols, alpha=0.85, edgecolor='white', width=0.6)
    for bar, val in zip(bars, pnl_vals):
        ax_bar.text(bar.get_x() + bar.get_width() / 2,
                    val + (500 if val >= 0 else -500),
                    f"{'+'if val>=0 else ''}${val/1000:.0f}K",
                    ha='center', va='bottom' if val >= 0 else 'top',
                    fontsize=9, fontweight='bold')
    ax_bar.axhline(0, color='black', linewidth=0.8)
    ax_bar.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v/1000:.0f}K'))
    ax_bar.set_title('Realized P&L by Year', fontsize=10)
    ax_bar.grid(axis='y', alpha=0.25)
    ax_bar.set_xticks(years)

    plt.tight_layout()
    out = Path('backtest_yearly_pnl.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved: {out}")


def save_monthly_symbols_chart(state: BacktestState):
    if not state.open_events or not state.trades:
        return

    opens  = pd.DataFrame(state.open_events)
    closes = pd.DataFrame(state.trades)
    opens['ym']  = pd.to_datetime(opens['date']).dt.to_period('M')
    closes['ym'] = pd.to_datetime(closes['date']).dt.to_period('M')
    all_months   = sorted(set(opens['ym'].tolist() + closes['ym'].tolist()))

    ABBREV = {
        'put_expired': 'exp✓', 'put_profit_take': 'PT✓',
        'put_assigned': 'asgn↓', 'call_expired': 'exp✓',
        'call_profit_take': 'PT✓', 'call_assigned': 'CC↑',
        'shares_expired': 'held',
    }

    rows = []
    for ym in all_months:
        mo_opens  = opens[opens['ym'] == ym]
        mo_closes = closes[closes['ym'] == ym]

        open_parts = []
        for _, r in mo_opens.iterrows():
            tag = 'p' if r['type'] == 'put' else 'c'
            n   = int(r['contracts'])
            s   = f"{r['ticker']} ${r['strike']:.0f}{tag}"
            if n > 1:
                s += f"×{n}"
            open_parts.append(s)

        close_parts = []
        for _, r in mo_closes.iterrows():
            sign = '+' if r['pnl'] >= 0 else ''
            abbr = ABBREV.get(r['type'], r['type'])
            close_parts.append(f"{r['ticker']} {sign}${r['pnl']:,.0f} ({abbr})")

        rows.append([str(ym),
                     ', '.join(open_parts)  if open_parts  else '—',
                     ', '.join(close_parts) if close_parts else '—'])

    cols = ['Month', 'Opened', 'Closed  (P&L · outcome)']

    # Split into two pages of ~22 rows each so text is readable
    chunk = 22
    pages = [rows[i:i+chunk] for i in range(0, len(rows), chunk)]

    for p_idx, page in enumerate(pages):
        fig, ax = plt.subplots(figsize=(20, max(4, len(page) * 0.55 + 1.2)))
        ax.axis('off')
        fig.suptitle(
            f'Monthly Symbol Log  (page {p_idx+1}/{len(pages)}) — Baseline strategy',
            fontsize=12, fontweight='bold'
        )

        tbl = ax.table(cellText=page, colLabels=cols, loc='center', cellLoc='left')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8.2)
        tbl.scale(1, 1.55)
        tbl.auto_set_column_width([0, 1, 2])

        # Header
        for j in range(len(cols)):
            tbl[(0, j)].set_facecolor('#2c3e50')
            tbl[(0, j)].set_text_props(color='white', fontweight='bold')

        # Alternating row shading + colour closed cell by net P&L
        for i, row in enumerate(page):
            bg = '#f2f3f4' if i % 2 == 0 else 'white'
            tbl[(i+1, 0)].set_facecolor('#eaf4fb')   # month column always blue-tint
            tbl[(i+1, 1)].set_facecolor(bg)

            # Parse net P&L for the closed column to pick colour
            closed_text = row[2]
            if closed_text == '—':
                tbl[(i+1, 2)].set_facecolor(bg)
            else:
                # Sum all P&L values in the cell
                import re
                nums = re.findall(r'([+-]?\$[\d,]+)', closed_text)
                net  = sum(int(n.replace('$','').replace(',','')) for n in nums)
                tbl[(i+1, 2)].set_facecolor('#d5f5e3' if net >= 0 else '#fadbd8')

        plt.tight_layout()
        out = Path(f'backtest_monthly_symbols_p{p_idx+1}.png')
        plt.savefig(out, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"Saved: {out}")


# ── Idle capital chart ────────────────────────────────────────────────────────

def save_idle_capital_chart(states: list, cfgs: list[StrategyConfig]):
    fig, axes = plt.subplots(len(states), 1,
                             figsize=(16, 5 * len(states)),
                             sharex=False)
    if len(states) == 1:
        axes = [axes]

    COLORS = ['#3498db', '#2ecc71']

    for ax, state, cfg, col in zip(axes, states, cfgs, COLORS):
        if not state.idle_log:
            continue

        idle_df = pd.DataFrame(state.idle_log,
                               columns=['date', 'idle_cash', 'put_col', 'share_val', 'idle_slots'])
        idle_df['date']     = pd.to_datetime(idle_df['date'])
        idle_df['deployed'] = idle_df['put_col'] + idle_df['share_val']
        idle_df['total']    = idle_df['deployed'] + idle_df['idle_cash']

        x = np.arange(len(idle_df))

        # Stacked bar: deployed (put collateral + shares) vs idle
        ax.bar(x, idle_df['put_col'],   color='#2ecc71', alpha=0.85, label='Put collateral')
        ax.bar(x, idle_df['share_val'], color='#f39c12', alpha=0.85,
               bottom=idle_df['put_col'], label='Shares held')
        ax.bar(x, idle_df['idle_cash'], color='#bdc3c7', alpha=0.70,
               bottom=idle_df['deployed'], label='Idle cash (undeployed)')

        # Line: idle cash as $ (right axis)
        ax2 = ax.twinx()
        ax2.plot(x, idle_df['idle_cash'], color='#e74c3c', linewidth=1.8,
                 linestyle='--', label='Idle $')
        ax2.set_ylabel('Idle Cash ($)', color='#e74c3c', fontsize=9)
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
        ax2.tick_params(axis='y', colors='#e74c3c')

        # Annotate avg idle and total left on table
        avg_idle  = idle_df['idle_cash'].mean()
        total_opp = idle_df['idle_cash'].sum()   # sum of monthly idle pots
        ax.text(0.99, 0.97,
                f"Avg idle/month: ${avg_idle:,.0f}\nTotal opportunity: ${total_opp:,.0f}",
                transform=ax.transAxes, ha='right', va='top',
                fontsize=9, color='#c0392b',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#fadbd8', alpha=0.8))

        # X-axis every 6 months
        tick_idx = list(range(0, len(idle_df), 6))
        ax.set_xticks(tick_idx)
        ax.set_xticklabels(
            [str(idle_df['date'].iloc[i])[:7] for i in tick_idx],
            rotation=35, ha='right', fontsize=8
        )
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
        ax.set_ylabel('Capital ($)')
        ax.set_title(f'Capital Utilisation — {cfg.name}', fontsize=11)

        # Combined legend
        handles1, labels1 = ax.get_legend_handles_labels()
        handles2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(handles1 + handles2, labels1 + labels2,
                  loc='upper left', fontsize=8, ncol=2)

    fig.suptitle('Monthly Capital Deployment: how much sat idle each month',
                 fontsize=13, fontweight='bold')
    plt.tight_layout()
    out = Path('backtest_idle_capital.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved: {out}")


# ── Comparison chart ──────────────────────────────────────────────────────────
def plot_comparison(results: list[dict]):
    COLORS = ['#95a5a6', '#3498db', '#2ecc71', '#e74c3c']

    fig = plt.figure(figsize=(16, 13))
    gs  = fig.add_gridspec(3, 2, hspace=0.42, wspace=0.32)
    ax_eq  = fig.add_subplot(gs[0, :])      # equity curves (full width)
    ax_mo  = fig.add_subplot(gs[1, :])      # monthly returns side-by-side
    ax_bar = fig.add_subplot(gs[2, 0])      # summary bar chart
    ax_tbl = fig.add_subplot(gs[2, 1])      # stats table

    fig.suptitle('Wheel Strategy — Strategy Comparison  (ORATS real fills, $100K)',
                 fontsize=14, fontweight='bold')

    # ── Equity curves ──────────────────────────────────────────────────────
    for res, col in zip(results, COLORS):
        eq = res['equity']
        ax_eq.plot(eq['date'], eq['value'], color=col, linewidth=2,
                   label=f"{res['name']}  ({res['total_ret']*100:+.1f}%)")
    ax_eq.axhline(STARTING_CAPITAL, color='black', linestyle=':', alpha=0.4)
    ax_eq.set_ylabel('Portfolio Value')
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_eq.legend(fontsize=8.5, loc='upper left')
    ax_eq.grid(alpha=0.25)
    ax_eq.set_title('Equity Curves', fontsize=11)

    # ── Monthly returns overlay (use equity_mo so bars are one-per-month) ──
    x_dates = results[0]['equity_mo']['date'].values
    n       = len(x_dates)
    width   = 0.22
    offsets = [-1.5, -0.5, 0.5, 1.5]

    for idx, (res, col, off) in enumerate(zip(results, COLORS, offsets)):
        mo_ret = res['equity_mo']['ret'].fillna(0) * 100
        xs     = np.arange(n) + off * width
        ax_mo.bar(xs, mo_ret.values, width=width, color=col, alpha=0.75,
                  label=res['name'].split('(')[0].strip())

    ax_mo.axhline(0, color='black', linewidth=0.8)
    ax_mo.axhline(1.0, color='orange', linestyle='--', linewidth=1, label='1% target')
    # X-axis: every 6th month
    tick_idx = list(range(0, n, 6))
    ax_mo.set_xticks(tick_idx)
    ax_mo.set_xticklabels(
        [str(results[0]['equity_mo']['date'].iloc[i])[:7] for i in tick_idx],
        rotation=35, ha='right', fontsize=7.5
    )
    ax_mo.set_ylabel('Monthly Return (%)')
    ax_mo.legend(fontsize=7.5, ncol=3)
    ax_mo.grid(axis='y', alpha=0.25)
    ax_mo.set_title('Monthly Returns — All Strategies', fontsize=11)

    # ── Summary bar chart ──────────────────────────────────────────────────
    names      = [r['name'].replace('  ', '\n') for r in results]
    ann_rets   = [r['ann_ret'] * 100 for r in results]
    bar_colors = [col if v >= 0 else '#e74c3c' for v, col in zip(ann_rets, COLORS)]
    bars = ax_bar.bar(names, ann_rets, color=bar_colors, alpha=0.85, edgecolor='white')
    for bar, val in zip(bars, ann_rets):
        ax_bar.text(bar.get_x() + bar.get_width() / 2,
                    val + 0.1 if val >= 0 else val - 0.3,
                    f'{val:.1f}%',
                    ha='center', va='bottom' if val >= 0 else 'top',
                    fontsize=9, fontweight='bold')
    ax_bar.axhline(0, color='black', linewidth=0.8)
    ax_bar.set_ylabel('Annual Return (%)')
    ax_bar.set_title('Annual Return by Strategy', fontsize=11)
    ax_bar.tick_params(axis='x', labelsize=7.5)
    ax_bar.grid(axis='y', alpha=0.25)

    # ── Stats table ────────────────────────────────────────────────────────
    ax_tbl.axis('off')
    cols   = ['Ann Ret', 'Avg Mo', 'Win%', 'Sharpe', 'Worst Mo', 'Trades']
    rows   = [r['name'].split('(')[0].strip() for r in results]
    data   = [
        [f"{r['ann_ret']*100:.1f}%",
         f"{r['avg_mo']:.2f}%",
         f"{r['win_rate']:.0f}%",
         f"{r['sharpe']:.2f}",
         f"{r['worst_mo']:.1f}%",
         str(r['n_trades'])]
        for r in results
    ]
    tbl = ax_tbl.table(cellText=data, rowLabels=rows, colLabels=cols,
                       loc='center', cellLoc='center')
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)
    tbl.scale(1.1, 1.6)
    # Colour row labels to match equity curves
    for i, col in zip(range(len(results)), COLORS):
        tbl[(i + 1, -1)].set_facecolor(col)
        tbl[(i + 1, -1)].set_text_props(color='white', fontweight='bold')
    ax_tbl.set_title('Summary Statistics', fontsize=11, pad=12)

    out = Path('backtest_comparison.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\nComparison chart saved → {out}")


# ── Entry point ───────────────────────────────────────────────────────────────
def run_backtest():
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}. Run ingest_orats.py first.")
        return

    con       = duckdb.connect(str(DB_PATH), read_only=True)
    build_ivr_table(con)
    all_dates = get_trading_dates(con)

    if not all_dates:
        print("No data in DB yet.")
        con.close()
        return

    print(f"\nPeriod: {all_dates[0]} → {all_dates[-1]}  |  "
          f"Capital: ${STARTING_CAPITAL:,}  |  "
          f"Delta: {TARGET_DELTA_LOW}–{TARGET_DELTA_HIGH}  |  DTE: {DTE_MIN}–{DTE_MAX}")

    all_states = []
    results    = []
    for i, cfg in enumerate(STRATEGIES):
        print(f"\nRunning: {cfg.name} ...")
        state = run_one(con, all_dates, cfg)
        all_states.append(state)
        results.append(summarise(state, cfg))
        if i == 0:
            save_yearly_pnl_chart(state)
            save_monthly_symbols_chart(state)

    con.close()

    # Save data for all strategies (slug = first word of name, lowercased)
    for i, (state, res) in enumerate(zip(all_states, results)):
        slug = STRATEGIES[i].name.split()[0].lower().rstrip(',')
        pd.DataFrame(res['equity']).to_csv(f'data/backtest_equity_{slug}.csv', index=False)
        if state.trades:
            pd.DataFrame(state.trades).to_csv(f'data/backtest_trades_{slug}.csv', index=False)
        if state.open_events:
            pd.DataFrame(state.open_events).to_csv(f'data/backtest_opens_{slug}.csv', index=False)

    # Keep legacy path for charts.py compatibility
    pd.DataFrame(results[0]['equity']).to_csv('data/backtest_equity.csv', index=False)

    save_idle_capital_chart(all_states, STRATEGIES)
    plot_comparison(results)


if __name__ == '__main__':
    run_backtest()
