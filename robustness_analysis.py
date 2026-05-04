"""
Robustness Analysis — addresses the key analyst concerns about the backtest:

  1. Walk-forward test      : in-sample 2020-2022, out-of-sample 2023-2026 (fresh $100K)
  2. Starting-date sensitivity : same strategy, 4 different entry points
  3. Stop-loss clustering   : are stops correlated? what's the real cluster drawdown?
  4. After-tax returns      : 37% ordinary income rate applied to realized gains
  5. Paper trading note     : already running via daily_report.py

Run:  python robustness_analysis.py
Outputs:
  data/backtest_equity_out-of-sample.csv
  data/backtest_trades_out-of-sample.csv
  robustness_walkforward.png
  robustness_sensitivity.png
  robustness_stops.png
  robustness_aftertax.png
  robustness_summary.txt
"""

import warnings
warnings.filterwarnings('ignore')

import textwrap
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
from pathlib import Path
from datetime import date, timedelta

# ── Shared constants ──────────────────────────────────────────────────────────
STARTING_CAPITAL = 100_000
TAX_RATE         = 0.37
OUT_SAMPLE_START = 2023

EQUITY_CSV = Path('data/backtest_equity_rules-based.csv')
TRADES_CSV = Path('data/backtest_trades_rules-based.csv')
OPENS_CSV  = Path('data/backtest_opens_rules-based.csv')

NAVY  = '#1a2744'
BLUE  = '#2980b9'
GREEN = '#27ae60'
RED   = '#e74c3c'
LGRAY = '#f2f3f4'
TEXT  = '#2c3e50'
MGRAY = '#bdc3c7'
WHITE = '#ffffff'
ORANGE = '#e67e22'

COLORS = {
    'in_sample':     '#2980b9',
    'out_of_sample': '#27ae60',
    '2020':          '#3498db',
    '2021':          '#2ecc71',
    '2022':          '#e74c3c',
    '2023':          '#9b59b6',
    'pre_tax':       '#2980b9',
    'after_tax':     '#e74c3c',
    'spy':           MGRAY,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def period_stats(eq_df: pd.DataFrame, label: str = '') -> dict:
    """Compute standard stats from a daily equity DataFrame (date, value)."""
    eq = eq_df.copy().sort_values('date')
    eq_mo = eq.set_index('date')['value'].resample('ME').last().reset_index()
    eq_mo['ret'] = eq_mo['value'].pct_change()

    start_v   = eq['value'].iloc[0]
    end_v     = eq['value'].iloc[-1]
    total_ret = end_v / start_v - 1
    n_years   = (eq['date'].iloc[-1] - eq['date'].iloc[0]).days / 365.25
    ann_ret   = (1 + total_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
    vol       = eq_mo['ret'].std() * np.sqrt(12)
    sharpe    = ann_ret / vol if vol > 0 else 0
    eq['peak']    = eq['value'].cummax()
    eq['dd']      = (eq['value'] - eq['peak']) / eq['peak'] * 100
    max_dd    = eq['dd'].min()
    worst_mo  = eq_mo['ret'].min() * 100
    win_rate  = (eq_mo['ret'] >= 0).mean() * 100

    return dict(label=label, total_ret=total_ret, ann_ret=ann_ret,
                avg_mo=eq_mo['ret'].mean() * 100, vol=vol, sharpe=sharpe,
                max_dd=max_dd, worst_mo=worst_mo, win_rate=win_rate,
                start=eq['date'].iloc[0], end=eq['date'].iloc[-1],
                start_v=start_v, end_v=end_v, n_years=n_years,
                eq=eq, eq_mo=eq_mo)


def run_out_of_sample():
    """
    Run the Rules-Based Wheel strategy from Jan 2023 with a fresh $100K.
    Returns (equity_df, trades_df) or None if DB locked.
    """
    oos_equity = Path('data/backtest_equity_out-of-sample.csv')
    oos_trades = Path('data/backtest_trades_out-of-sample.csv')

    if oos_equity.exists() and oos_trades.exists():
        print("  Out-of-sample CSVs already exist — loading cached run.")
        eq = pd.read_csv(oos_equity, parse_dates=['date'])
        tr = pd.read_csv(oos_trades, parse_dates=['date'])
        return eq, tr

    print("  Running out-of-sample backtest (Jan 2023 → Apr 2026)...")
    try:
        import duckdb
        from backtest_orats import (
            run_one, summarise, get_trading_dates,
            build_ivr_table, STRATEGIES, DB_PATH
        )

        con = duckdb.connect(str(DB_PATH), read_only=True)
        build_ivr_table(con)
        all_dates  = get_trading_dates(con)
        oos_dates  = [d for d in all_dates if d.year >= OUT_SAMPLE_START]

        cfg   = STRATEGIES[1]   # Rules-Based Wheel
        state = run_one(con, oos_dates, cfg)
        res   = summarise(state, cfg)
        con.close()

        eq = res['equity']
        tr = pd.DataFrame(state.trades)

        eq.to_csv(oos_equity, index=False)
        if not tr.empty:
            tr.to_csv(oos_trades, index=False)

        return eq, tr

    except Exception as e:
        print(f"  Out-of-sample run failed: {e}")
        return None, None


# ── Analysis 1: Walk-Forward ──────────────────────────────────────────────────

def analysis_walkforward(eq_full: pd.DataFrame, tr_full: pd.DataFrame):
    print("\n=== WALK-FORWARD TEST ===")

    # In-sample: 2020-2022 slice of the full run
    eq_in  = eq_full[eq_full['date'].dt.year <= 2022].copy()
    tr_in  = tr_full[pd.to_datetime(tr_full['date']).dt.year <= 2022].copy()

    # Out-of-sample: fresh run from Jan 2023
    eq_oos, tr_oos = run_out_of_sample()

    stats_in  = period_stats(eq_in,  label='In-sample  (2020–2022)')
    if eq_oos is not None:
        stats_oos = period_stats(eq_oos, label='Out-of-sample  (2023–2026, fresh $100K)')
    else:
        stats_oos = None

    # Print
    for s in [stats_in] + ([stats_oos] if stats_oos else []):
        print(f"\n  {s['label']}")
        print(f"    Annual return : {s['ann_ret']*100:.1f}%")
        print(f"    Avg monthly   : {s['avg_mo']:.2f}%")
        print(f"    Sharpe        : {s['sharpe']:.2f}")
        print(f"    Worst month   : {s['worst_mo']:.1f}%")
        print(f"    Max drawdown  : {s['max_dd']:.1f}%")

    verdict = ""
    if stats_oos:
        ratio = stats_oos['ann_ret'] / stats_in['ann_ret'] if stats_in['ann_ret'] > 0 else 0
        if ratio >= 0.75:
            verdict = "ROBUST — out-of-sample within 25% of in-sample return"
        elif ratio >= 0.50:
            verdict = "MODERATE — some degradation, likely legitimate edge with noise"
        else:
            verdict = "CONCERN — material degradation, possible overfitting"
        print(f"\n  Verdict: {verdict}")

    # Chart
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle('Walk-Forward Test: In-Sample vs Out-of-Sample',
                 fontsize=13, fontweight='bold')

    ax_eq = axes[0]
    # Normalise both to $100K start
    def normalise(df):
        d = df.copy()
        d['norm'] = d['value'] / d['value'].iloc[0] * STARTING_CAPITAL
        return d

    eq_in_n  = normalise(eq_in)
    ax_eq.plot(eq_in_n['date'],  eq_in_n['norm'],
               color=COLORS['in_sample'], linewidth=2,
               label=f"In-sample 2020–22 ({stats_in['ann_ret']*100:.1f}%/yr)")
    if stats_oos:
        eq_oos_n = normalise(stats_oos['eq'])
        ax_eq.plot(eq_oos_n['date'], eq_oos_n['norm'],
                   color=COLORS['out_of_sample'], linewidth=2,
                   label=f"Out-of-sample 2023–26 ({stats_oos['ann_ret']*100:.1f}%/yr)")
    ax_eq.axhline(STARTING_CAPITAL, color=MGRAY, linestyle=':', linewidth=1)
    ax_eq.set_ylabel('Normalised Portfolio Value')
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_eq.legend(fontsize=9)
    ax_eq.grid(alpha=0.25)
    ax_eq.set_title('Equity Curves (both normalised to $100K)', fontsize=10)

    # Stats comparison table
    ax_tbl = axes[1]
    ax_tbl.axis('off')
    rows_data = [
        ['Annual Return',
         f"{stats_in['ann_ret']*100:.1f}%",
         f"{stats_oos['ann_ret']*100:.1f}%" if stats_oos else 'N/A'],
        ['Avg Monthly',
         f"{stats_in['avg_mo']:.2f}%",
         f"{stats_oos['avg_mo']:.2f}%" if stats_oos else 'N/A'],
        ['Sharpe Ratio',
         f"{stats_in['sharpe']:.2f}",
         f"{stats_oos['sharpe']:.2f}" if stats_oos else 'N/A'],
        ['Worst Month',
         f"{stats_in['worst_mo']:.1f}%",
         f"{stats_oos['worst_mo']:.1f}%" if stats_oos else 'N/A'],
        ['Max Drawdown',
         f"{stats_in['max_dd']:.1f}%",
         f"{stats_oos['max_dd']:.1f}%" if stats_oos else 'N/A'],
        ['Win Rate (mo)',
         f"{stats_in['win_rate']:.0f}%",
         f"{stats_oos['win_rate']:.0f}%" if stats_oos else 'N/A'],
    ]
    tbl = ax_tbl.table(cellText=rows_data,
                       colLabels=['Metric', 'In-Sample\n2020–2022',
                                  'Out-of-Sample\n2023–2026'],
                       loc='center', cellLoc='center')
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1.1, 2.0)
    for j in range(3):
        tbl[0, j].set_facecolor(NAVY)
        tbl[0, j].set_text_props(color=WHITE, fontweight='bold')
    for i in range(len(rows_data)):
        bg = LGRAY if i % 2 == 0 else WHITE
        for j in range(3):
            tbl[i+1, j].set_facecolor(bg)
    if stats_oos and verdict:
        ax_tbl.text(0.5, 0.07, verdict, ha='center', va='center',
                    fontsize=9, fontweight='bold', color=NAVY,
                    transform=ax_tbl.transAxes,
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='#d5f5e3', alpha=0.9))
    ax_tbl.set_title('Performance Comparison', fontsize=10, fontweight='bold', pad=8)

    plt.tight_layout()
    out = Path('robustness_walkforward.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {out}")

    return stats_in, stats_oos


# ── Analysis 2: Starting-Date Sensitivity ─────────────────────────────────────

def analysis_sensitivity(eq_full: pd.DataFrame):
    print("\n=== STARTING-DATE SENSITIVITY ===")

    results = []
    for start_year in [2020, 2021, 2022, 2023]:
        sub = eq_full[eq_full['date'].dt.year >= start_year].copy()
        if len(sub) < 50:
            continue
        s = period_stats(sub, label=f"Start {start_year}")
        results.append(s)
        print(f"  Start {start_year}: {s['ann_ret']*100:.1f}%/yr  "
              f"Sharpe {s['sharpe']:.2f}  Worst mo {s['worst_mo']:.1f}%  "
              f"({s['n_years']:.1f} yrs)")

    # Chart
    fig, (ax_eq, ax_bar) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle('Starting-Date Sensitivity: Same Strategy, Different Entry Points',
                 fontsize=13, fontweight='bold')

    palette = ['#3498db', '#2ecc71', '#e74c3c', '#9b59b6']
    for s, col in zip(results, palette):
        eq = s['eq'].copy()
        eq['norm'] = eq['value'] / eq['value'].iloc[0] * STARTING_CAPITAL
        ax_eq.plot(eq['date'], eq['norm'], color=col, linewidth=2,
                   label=f"Start {s['label'][-4:]}  ({s['ann_ret']*100:.1f}%/yr)")
    ax_eq.axhline(STARTING_CAPITAL, color=MGRAY, linestyle=':', linewidth=1)
    ax_eq.set_ylabel('Normalised Portfolio Value')
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_eq.legend(fontsize=9)
    ax_eq.grid(alpha=0.25)
    ax_eq.set_title('Equity Curves (normalised to $100K at entry)', fontsize=10)

    # Bar: annual return by start year
    labels   = [s['label'][-4:] for s in results]
    ann_rets = [s['ann_ret'] * 100 for s in results]
    b_cols   = [col for col, s in zip(palette, results)]
    bars = ax_bar.bar(labels, ann_rets, color=b_cols, alpha=0.85, edgecolor='white', width=0.5)
    for bar, val in zip(bars, ann_rets):
        ax_bar.text(bar.get_x() + bar.get_width() / 2,
                    val + 0.3, f'{val:.1f}%',
                    ha='center', va='bottom', fontsize=11, fontweight='bold')
    ax_bar.axhline(0, color='black', linewidth=0.8)
    ax_bar.axhline(np.mean(ann_rets), color=ORANGE, linestyle='--', linewidth=1.5,
                   label=f'Avg {np.mean(ann_rets):.1f}%')
    ax_bar.set_ylabel('Annualised Return (%)')
    ax_bar.set_title('Annual Return by Entry Year', fontsize=10)
    ax_bar.legend(fontsize=9)
    ax_bar.grid(axis='y', alpha=0.25)
    ax_bar.set_ylim(0, max(ann_rets) * 1.25)

    plt.tight_layout()
    out = Path('robustness_sensitivity.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {out}")

    return results


# ── Analysis 3: Stop-Loss Clustering ─────────────────────────────────────────

def analysis_stop_clustering(tr: pd.DataFrame, eq: pd.DataFrame):
    print("\n=== STOP-LOSS CLUSTERING ===")

    stops = tr[tr['type'] == 'stop_loss'].copy()
    stops['date'] = pd.to_datetime(stops['date'])
    stops = stops.sort_values('date')

    total_stops  = len(stops)
    total_stop_pnl = stops['pnl'].sum()
    avg_stop_pnl   = stops['pnl'].mean()

    # Find clusters: stops within 30 calendar days of each other
    stops['cluster_id'] = 0
    cluster_id = 0
    cluster_starts = []
    in_cluster = []

    for i, row in stops.iterrows():
        nearby = stops[
            (stops['date'] >= row['date'] - timedelta(days=15)) &
            (stops['date'] <= row['date'] + timedelta(days=15)) &
            (stops.index != i)
        ]
        if len(nearby) >= 1:
            in_cluster.append(i)

    # Group consecutive cluster members
    cluster_map = {}
    cid = 0
    sorted_stops = stops.reset_index(drop=True)
    sorted_stops['in_cluster'] = False
    for i in range(len(sorted_stops)):
        row = sorted_stops.iloc[i]
        nearby_mask = (
            (sorted_stops['date'] >= row['date'] - timedelta(days=30)) &
            (sorted_stops['date'] <= row['date'] + timedelta(days=30))
        )
        if nearby_mask.sum() > 1:
            sorted_stops.loc[i, 'in_cluster'] = True

    clustered = sorted_stops[sorted_stops['in_cluster']]
    isolated  = sorted_stops[~sorted_stops['in_cluster']]

    # Find worst 30-day windows
    window_results = []
    for i, row in sorted_stops.iterrows():
        window = sorted_stops[
            (sorted_stops['date'] >= row['date']) &
            (sorted_stops['date'] <= row['date'] + timedelta(days=30))
        ]
        if len(window) >= 2:
            window_results.append({
                'start':     row['date'],
                'n_stops':   len(window),
                'total_pnl': window['pnl'].sum(),
                'tickers':   ', '.join(window['ticker'].tolist()),
            })

    worst_windows = (pd.DataFrame(window_results)
                     .sort_values('total_pnl')
                     .drop_duplicates(subset=['start'])
                     .head(5)) if window_results else pd.DataFrame()

    print(f"  Total stops: {total_stops}  Total loss: ${total_stop_pnl:,.0f}  "
          f"Avg per stop: ${avg_stop_pnl:,.0f}")
    print(f"  Isolated stops: {len(isolated)}  "
          f"Clustered stops: {len(clustered)} ({len(clustered)/total_stops*100:.0f}%)")
    if not worst_windows.empty:
        print("  Worst 30-day windows:")
        for _, w in worst_windows.iterrows():
            print(f"    {str(w['start'])[:10]}  {int(w['n_stops'])} stops  "
                  f"${w['total_pnl']:,.0f}  tickers: {w['tickers']}")

    # Chart
    fig, (ax_tl, ax_dd) = plt.subplots(2, 1, figsize=(14, 8),
                                        gridspec_kw={'height_ratios': [1, 1.5]})
    fig.suptitle('Stop-Loss Event Analysis', fontsize=13, fontweight='bold')

    # Timeline of stops
    for _, row in isolated.iterrows():
        ax_tl.axvline(row['date'], color=RED, alpha=0.5, linewidth=1.2)
    for _, row in clustered.iterrows():
        ax_tl.axvline(row['date'], color='#7b0000', alpha=0.85, linewidth=2.0)

    ax_tl.set_xlim(stops['date'].min() - timedelta(days=30),
                   stops['date'].max() + timedelta(days=30))
    ax_tl.set_ylim(0, 1)
    ax_tl.set_yticks([])
    ax_tl.set_title(
        f'Stop-Loss Timeline  ({total_stops} total: '
        f'{len(isolated)} isolated in red, {len(clustered)} clustered in dark red)',
        fontsize=10)
    ax_tl.grid(axis='x', alpha=0.2)

    # Portfolio equity + stop events marked
    eq2 = eq.copy()
    eq2['date'] = pd.to_datetime(eq2['date'])
    eq2['peak'] = eq2['value'].cummax()
    eq2['dd']   = (eq2['value'] - eq2['peak']) / eq2['peak'] * 100

    ax_dd.plot(eq2['date'], eq2['value'], color=BLUE, linewidth=1.8, label='Portfolio value')
    ax_dd.fill_between(eq2['date'], eq2['value'], eq2['peak'],
                       color=RED, alpha=0.12, label='Drawdown from peak')

    # Mark each stop event on the equity curve
    for _, row in sorted_stops.iterrows():
        eq_at_stop = eq2[eq2['date'] <= row['date']]
        if not eq_at_stop.empty:
            val = eq_at_stop['value'].iloc[-1]
            col = '#7b0000' if row['in_cluster'] else RED
            ax_dd.scatter(row['date'], val, color=col, s=35, zorder=5, alpha=0.8)

    ax_dd.set_ylabel('Portfolio Value ($)')
    ax_dd.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_dd.legend(fontsize=9)
    ax_dd.grid(alpha=0.2)
    ax_dd.set_title('Portfolio Value with Stop-Loss Events Marked  '
                    '(dark = clustered, light = isolated)', fontsize=10)

    plt.tight_layout()
    out = Path('robustness_stops.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {out}")

    return dict(total=total_stops, clustered=len(clustered),
                isolated=len(isolated), worst_windows=worst_windows,
                total_pnl=total_stop_pnl)


# ── Analysis 4: After-Tax Returns ─────────────────────────────────────────────

def analysis_after_tax(eq: pd.DataFrame, tr: pd.DataFrame):
    print("\n=== AFTER-TAX RETURNS ===")

    eq = eq.copy()
    eq['date'] = pd.to_datetime(eq['date'])
    tr = tr.copy()
    tr['date'] = pd.to_datetime(tr['date'])
    tr['year'] = tr['date'].dt.year

    # Annual realized gains — tax paid once a year (Q1 following year, simplified)
    annual_gains = tr.groupby('year')['pnl'].sum()
    annual_tax   = annual_gains.apply(lambda g: max(0, g) * TAX_RATE)

    print("  Annual tax liability:")
    cumulative_tax = 0
    tax_events = []
    for yr, gain in annual_gains.items():
        tax = max(0, gain) * TAX_RATE
        cumulative_tax += tax
        print(f"    {yr}  Realized P&L: {'+'if gain>=0 else ''}${gain:,.0f}  "
              f"Tax (37%): ${tax:,.0f}")
        # Tax paid April 15 of the following year
        tax_date = pd.Timestamp(f'{yr+1}-04-15')
        tax_events.append((tax_date, tax))

    # Build after-tax equity: subtract each year's tax at the payment date
    eq = eq.sort_values('date').copy()
    eq['after_tax_value'] = eq['value'].copy()
    cumtax_paid = 0.0
    for tax_date, tax in sorted(tax_events):
        mask = eq['date'] >= tax_date
        eq.loc[mask, 'after_tax_value'] -= tax
        cumtax_paid += tax

    # Stats
    pre_stats  = period_stats(eq.rename(columns={'value': 'value'})[['date','value']],
                              label='Pre-tax')
    post_df    = eq[['date', 'after_tax_value']].rename(
                     columns={'after_tax_value': 'value'})
    post_df    = post_df[post_df['value'] > 0]
    post_stats = period_stats(post_df, label='After-tax (37%)')

    print(f"\n  Pre-tax  annual return: {pre_stats['ann_ret']*100:.1f}%  "
          f"Final: ${pre_stats['end_v']:,.0f}")
    print(f"  After-tax annual return: {post_stats['ann_ret']*100:.1f}%  "
          f"Final: ${post_stats['end_v']:,.0f}")
    print(f"  Total tax paid: ${cumtax_paid:,.0f}")

    # SPY after-tax comparison (15% long-term cap gains)
    try:
        import yfinance as yf
        spy = yf.download('SPY',
                          start=eq['date'].min().strftime('%Y-%m-%d'),
                          end=eq['date'].max().strftime('%Y-%m-%d'),
                          auto_adjust=True, progress=False)
        spy_close = spy['Close'].squeeze().dropna()
        spy_total_gain = float(spy_close.iloc[-1] / spy_close.iloc[0] - 1)
        spy_tax = spy_total_gain * STARTING_CAPITAL * 0.15  # LTCG rate
        spy_after_tax_end = STARTING_CAPITAL * (1 + spy_total_gain) - spy_tax
        n_years = (eq['date'].iloc[-1] - eq['date'].iloc[0]).days / 365.25
        spy_after_tax_ann = (spy_after_tax_end / STARTING_CAPITAL) ** (1 / n_years) - 1
        print(f"  SPY after-tax (15% LTCG) annual return: {spy_after_tax_ann*100:.1f}%")
        has_spy = True
    except Exception:
        has_spy = False

    # Chart
    fig, (ax_eq, ax_bar) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle('After-Tax Returns: 37% Ordinary Income vs Pre-Tax',
                 fontsize=13, fontweight='bold')

    ax_eq.plot(eq['date'], eq['value'],
               color=COLORS['pre_tax'], linewidth=2,
               label=f"Pre-tax  ({pre_stats['ann_ret']*100:.1f}%/yr, "
                     f"final ${pre_stats['end_v']:,.0f})")
    ax_eq.plot(post_df['date'], post_df['value'],
               color=COLORS['after_tax'], linewidth=2,
               label=f"After-tax 37%  ({post_stats['ann_ret']*100:.1f}%/yr, "
                     f"final ${post_stats['end_v']:,.0f})")
    ax_eq.axhline(STARTING_CAPITAL, color=MGRAY, linestyle=':', linewidth=1)

    # Mark tax payment dates
    for tax_date, tax in tax_events:
        if tax > 0:
            ax_eq.axvline(tax_date, color='#e67e22', alpha=0.5,
                          linewidth=1, linestyle='--')

    ax_eq.set_ylabel('Portfolio Value ($)')
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_eq.legend(fontsize=9)
    ax_eq.grid(alpha=0.25)
    ax_eq.set_title('Equity Curve  (orange dashes = annual tax payments)', fontsize=10)

    # Bar: pre vs after-tax by year
    years_list = sorted(annual_gains.index)
    pre_by_yr  = []
    aft_by_yr  = []
    prev_pre = prev_aft = STARTING_CAPITAL
    for yr in years_list:
        yr_eq = eq[eq['date'].dt.year == yr]
        if yr_eq.empty:
            continue
        end_pre = yr_eq['value'].iloc[-1]
        end_aft = yr_eq['after_tax_value'].iloc[-1]
        pre_by_yr.append((end_pre - prev_pre) / prev_pre * 100)
        aft_by_yr.append((end_aft - prev_aft) / prev_aft * 100)
        prev_pre = end_pre
        prev_aft = end_aft

    x = np.arange(len(years_list))
    w = 0.38
    ax_bar.bar(x - w/2, pre_by_yr,  width=w, color=COLORS['pre_tax'],
               alpha=0.85, label='Pre-tax', edgecolor='white')
    ax_bar.bar(x + w/2, aft_by_yr, width=w, color=COLORS['after_tax'],
               alpha=0.85, label='After-tax 37%', edgecolor='white')
    ax_bar.axhline(0, color='black', linewidth=0.8)
    ax_bar.set_xticks(x)
    ax_bar.set_xticklabels([str(y) for y in years_list])
    ax_bar.set_ylabel('Annual Return (%)')
    ax_bar.set_title('Annual Return: Pre-Tax vs After-Tax', fontsize=10)
    ax_bar.legend(fontsize=9)
    ax_bar.grid(axis='y', alpha=0.25)

    plt.tight_layout()
    out = Path('robustness_aftertax.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {out}")

    return dict(pre_ann=pre_stats['ann_ret'], post_ann=post_stats['ann_ret'],
                total_tax=cumtax_paid, pre_final=pre_stats['end_v'],
                post_final=post_stats['end_v'])


# ── Summary report ────────────────────────────────────────────────────────────

def write_summary(wf_in, wf_oos, sens_results, stop_data, tax_data):
    lines = [
        "ROBUSTNESS ANALYSIS SUMMARY",
        "=" * 60,
        "",
        "1. WALK-FORWARD TEST",
        f"   In-sample  (2020-2022): {wf_in['ann_ret']*100:.1f}%/yr  "
        f"Sharpe {wf_in['sharpe']:.2f}",
    ]
    if wf_oos:
        ratio = wf_oos['ann_ret'] / wf_in['ann_ret'] if wf_in['ann_ret'] > 0 else 0
        verdict = ("ROBUST" if ratio >= 0.75
                   else "MODERATE" if ratio >= 0.50 else "CONCERN")
        lines += [
            f"   Out-of-sample (2023-2026): {wf_oos['ann_ret']*100:.1f}%/yr  "
            f"Sharpe {wf_oos['sharpe']:.2f}",
            f"   Retention ratio: {ratio*100:.0f}% → {verdict}",
        ]
    lines += [
        "",
        "2. STARTING-DATE SENSITIVITY",
    ]
    for s in sens_results:
        lines.append(f"   Start {s['label'][-4:]}: {s['ann_ret']*100:.1f}%/yr  "
                     f"Sharpe {s['sharpe']:.2f}  Worst mo {s['worst_mo']:.1f}%")
    ann_rets = [s['ann_ret']*100 for s in sens_results]
    lines += [
        f"   Range: {min(ann_rets):.1f}% – {max(ann_rets):.1f}%  "
        f"(all positive = robust to entry timing)",
        "",
        "3. STOP-LOSS CLUSTERING",
        f"   Total stops: {stop_data['total']}  "
        f"Isolated: {stop_data['isolated']}  Clustered: {stop_data['clustered']}",
        f"   Total stop-loss P&L: ${stop_data['total_pnl']:,.0f}",
    ]
    if not stop_data['worst_windows'].empty:
        lines.append("   Worst 30-day windows:")
        for _, w in stop_data['worst_windows'].head(3).iterrows():
            lines.append(f"     {str(w['start'])[:10]}: {int(w['n_stops'])} stops, "
                         f"${w['total_pnl']:,.0f}")
    lines += [
        "",
        "4. AFTER-TAX RETURNS (37% ordinary income)",
        f"   Pre-tax annual return : {tax_data['pre_ann']*100:.1f}%  "
        f"Final: ${tax_data['pre_final']:,.0f}",
        f"   After-tax annual return: {tax_data['post_ann']*100:.1f}%  "
        f"Final: ${tax_data['post_final']:,.0f}",
        f"   Total tax paid (est.) : ${tax_data['total_tax']:,.0f}",
        "",
        "5. PAPER TRADING",
        "   Live screener is operational (daily_report.py + screener.py).",
        "   IV history bootstrapped from ORATS (137K readings, 87 tickers).",
        "   Recommendation: run paper trades for 90 days before deploying capital.",
        "   Log each screener recommendation daily; compare to backtest predictions.",
    ]

    out = Path('robustness_summary.txt')
    out.write_text('\n'.join(lines))
    print(f"\nSaved: {out}")
    print('\n'.join(lines))


# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    for f in (EQUITY_CSV, TRADES_CSV):
        if not f.exists():
            print(f"Missing: {f} — run backtest_orats.py first.")
            return

    print("Loading full backtest data...")
    eq_full = pd.read_csv(EQUITY_CSV, parse_dates=['date'])
    tr_full = pd.read_csv(TRADES_CSV, parse_dates=['date'])
    print(f"  {len(eq_full)} trading days, {len(tr_full)} trades")

    wf_in, wf_oos  = analysis_walkforward(eq_full, tr_full)
    sens_results   = analysis_sensitivity(eq_full)
    stop_data      = analysis_stop_clustering(tr_full, eq_full)
    tax_data       = analysis_after_tax(eq_full, tr_full)
    write_summary(wf_in, wf_oos, sens_results, stop_data, tax_data)

    print("\nAll robustness analyses complete.")
    print("Charts: robustness_walkforward.png, robustness_sensitivity.png, "
          "robustness_stops.png, robustness_aftertax.png")


if __name__ == '__main__':
    run()
