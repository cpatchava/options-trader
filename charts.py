"""
Backtest visualisation — reads data/backtest_trades.csv + data/backtest_equity.csv
and produces three charts:

  backtest_monthly_pnl.png       — Monthly P&L bars ($ per month, green/red)
  backtest_trade_breakdown.png   — Stacked monthly outcome counts + $ by type
  backtest_equity.png            — Equity curve with drawdown shading

Run:  python charts.py
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path

TRADES_CSV = Path('data/backtest_trades.csv')
EQUITY_CSV = Path('data/backtest_equity.csv')
STARTING_CAPITAL = 100_000

OUTCOME_COLORS = {
    'put_expired':      '#2ecc71',   # green
    'put_profit_take':  '#27ae60',   # dark green
    'put_assigned':     '#f39c12',   # orange (not a loss, but a transition)
    'call_expired':     '#3498db',   # blue
    'call_profit_take': '#2980b9',   # dark blue
    'call_assigned':    '#e74c3c',   # red (caps upside)
}

OUTCOME_LABELS = {
    'put_expired':      'Put expired OTM',
    'put_profit_take':  'Put profit-take',
    'put_assigned':     'Put assigned',
    'call_expired':     'Call expired OTM',
    'call_profit_take': 'Call profit-take',
    'call_assigned':    'Call assigned (capped)',
}


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_csv(TRADES_CSV, parse_dates=['date'])
    equity = pd.read_csv(EQUITY_CSV, parse_dates=['date'])
    trades['year_month'] = trades['date'].dt.to_period('M')
    return trades, equity


# ── Chart 1: Monthly P&L bars ──────────────────────────────────────────────────

def chart_monthly_pnl(trades: pd.DataFrame, equity: pd.DataFrame):
    # Compute month-over-month equity change as the P&L realised in that month
    equity = equity.copy().sort_values('date')
    equity['month'] = equity['date'].dt.to_period('M')
    equity['prev_value'] = equity['value'].shift(1)
    equity.loc[equity.index[0], 'prev_value'] = STARTING_CAPITAL
    equity['monthly_pnl'] = equity['value'] - equity['prev_value']

    months = equity['month'].astype(str)
    pnl    = equity['monthly_pnl']
    colors = ['#2ecc71' if v >= 0 else '#e74c3c' for v in pnl]

    fig, axes = plt.subplots(2, 1, figsize=(16, 10), gridspec_kw={'height_ratios': [1, 2]})
    fig.suptitle('Wheel Strategy — Monthly Performance  (ORATS real fills, $100K capital)',
                 fontsize=14, fontweight='bold', y=0.98)

    # Top: equity curve
    ax0 = axes[0]
    ax0.plot(equity['date'], equity['value'], color='steelblue', linewidth=2)
    ax0.axhline(STARTING_CAPITAL, color='gray', linestyle=':', linewidth=1, alpha=0.6)
    ax0.fill_between(equity['date'], STARTING_CAPITAL, equity['value'],
                     where=equity['value'] >= STARTING_CAPITAL,
                     color='#2ecc71', alpha=0.15, label='Above cost')
    ax0.fill_between(equity['date'], STARTING_CAPITAL, equity['value'],
                     where=equity['value'] < STARTING_CAPITAL,
                     color='#e74c3c', alpha=0.15, label='Below cost')
    ax0.set_ylabel('Portfolio Value')
    ax0.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax0.grid(alpha=0.25)
    ax0.legend(fontsize=8)

    # Bottom: monthly P&L bars
    ax1 = axes[1]
    x = np.arange(len(months))
    bars = ax1.bar(x, pnl, color=colors, alpha=0.85, edgecolor='white', linewidth=0.4)
    ax1.axhline(0, color='black', linewidth=0.8)
    ax1.axhline(pnl.mean(), color='orange', linestyle='--', linewidth=1.2,
                label=f'Avg {pnl.mean():+,.0f}/mo')

    # Label bars with $ amount (only for |value| > threshold to avoid clutter)
    threshold = pnl.abs().max() * 0.08
    for bar, val in zip(bars, pnl):
        if abs(val) > threshold:
            ax1.text(bar.get_x() + bar.get_width() / 2,
                     val + (200 if val >= 0 else -200),
                     f'${val:+,.0f}',
                     ha='center', va='bottom' if val >= 0 else 'top',
                     fontsize=6.5, fontweight='bold',
                     color='#1a5e1a' if val >= 0 else '#7b0000')

    # X-axis: show every 3rd month label
    tick_idx  = x[::3]
    tick_lbls = [months.iloc[i] for i in tick_idx]
    ax1.set_xticks(tick_idx)
    ax1.set_xticklabels(tick_lbls, rotation=35, ha='right', fontsize=8)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:+,.0f}'))
    ax1.set_ylabel('Monthly P&L ($)')
    ax1.set_xlabel('Month')
    ax1.legend(fontsize=9)
    ax1.grid(axis='y', alpha=0.25)

    # Annotate worst month
    worst_idx = pnl.idxmin()
    ax1.annotate(f'COVID crash\n${pnl[worst_idx]:+,.0f}',
                 xy=(worst_idx, pnl[worst_idx]),
                 xytext=(worst_idx + 3, pnl[worst_idx] - 2000),
                 fontsize=8, color='#7b0000',
                 arrowprops=dict(arrowstyle='->', color='#7b0000', lw=1.2))

    plt.tight_layout()
    out = Path('backtest_monthly_pnl.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {out}')


# ── Chart 2: Trade outcome breakdown ──────────────────────────────────────────

def chart_trade_breakdown(trades: pd.DataFrame):
    outcomes = list(OUTCOME_COLORS.keys())

    # Monthly P&L by outcome
    monthly = (trades.groupby(['year_month', 'type'])['pnl']
               .sum()
               .unstack(fill_value=0)
               .reindex(columns=outcomes, fill_value=0))

    # Monthly trade count by outcome
    monthly_cnt = (trades.groupby(['year_month', 'type'])
                   .size()
                   .unstack(fill_value=0)
                   .reindex(columns=outcomes, fill_value=0))

    months_str = [str(m) for m in monthly.index]
    x = np.arange(len(months_str))

    fig, axes = plt.subplots(2, 1, figsize=(16, 11), gridspec_kw={'height_ratios': [1.2, 1]})
    fig.suptitle('Trade Outcome Breakdown by Month', fontsize=14, fontweight='bold', y=0.99)

    # Top: stacked P&L by outcome type
    ax0 = axes[0]
    bottom_pos = np.zeros(len(x))
    bottom_neg = np.zeros(len(x))
    for col in outcomes:
        vals = monthly[col].values if col in monthly.columns else np.zeros(len(x))
        pos_vals = np.where(vals > 0, vals, 0)
        neg_vals = np.where(vals < 0, vals, 0)
        ax0.bar(x, pos_vals, bottom=bottom_pos, color=OUTCOME_COLORS[col],
                label=OUTCOME_LABELS[col], alpha=0.85, edgecolor='white', linewidth=0.3)
        ax0.bar(x, neg_vals, bottom=bottom_neg, color=OUTCOME_COLORS[col],
                alpha=0.85, edgecolor='white', linewidth=0.3)
        bottom_pos += pos_vals
        bottom_neg += neg_vals

    ax0.axhline(0, color='black', linewidth=0.9)
    ax0.set_ylabel('P&L ($)')
    ax0.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:+,.0f}'))
    ax0.set_xticks(x[::3])
    ax0.set_xticklabels([months_str[i] for i in range(0, len(months_str), 3)],
                        rotation=35, ha='right', fontsize=8)
    ax0.legend(loc='upper left', fontsize=7.5, ncol=2)
    ax0.grid(axis='y', alpha=0.25)
    ax0.set_title('Monthly P&L by Trade Outcome Type', fontsize=11)

    # Bottom: stacked count per month
    ax1 = axes[1]
    bot = np.zeros(len(x))
    for col in outcomes:
        cnts = monthly_cnt[col].values if col in monthly_cnt.columns else np.zeros(len(x))
        ax1.bar(x, cnts, bottom=bot, color=OUTCOME_COLORS[col],
                alpha=0.85, edgecolor='white', linewidth=0.3)
        bot += cnts

    ax1.set_ylabel('Trade Count')
    ax1.set_xticks(x[::3])
    ax1.set_xticklabels([months_str[i] for i in range(0, len(months_str), 3)],
                        rotation=35, ha='right', fontsize=8)
    ax1.grid(axis='y', alpha=0.25)
    ax1.set_xlabel('Month')
    ax1.set_title('Monthly Trade Count by Outcome Type', fontsize=11)

    plt.tight_layout()
    out = Path('backtest_trade_breakdown.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {out}')


# ── Chart 3: Equity curve with drawdown ───────────────────────────────────────

def chart_equity_drawdown(equity: pd.DataFrame):
    equity = equity.copy().sort_values('date')
    equity['peak']     = equity['value'].cummax()
    equity['drawdown'] = (equity['value'] - equity['peak']) / equity['peak'] * 100
    equity['ret_mo']   = equity['value'].pct_change() * 100

    fig, axes = plt.subplots(3, 1, figsize=(14, 11),
                             gridspec_kw={'height_ratios': [2.5, 1.2, 1]})
    fig.suptitle('Equity Curve, Drawdown & Monthly Returns', fontsize=13,
                 fontweight='bold', y=0.99)

    # Equity
    ax0 = axes[0]
    ax0.plot(equity['date'], equity['value'], color='steelblue', linewidth=2)
    ax0.fill_between(equity['date'], equity['value'], equity['peak'],
                     color='#e74c3c', alpha=0.18, label='Drawdown')
    ax0.axhline(STARTING_CAPITAL, color='gray', linestyle=':', alpha=0.6, label='Starting capital')
    ax0.set_ylabel('Portfolio Value')
    ax0.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax0.legend(fontsize=9)
    ax0.grid(alpha=0.25)

    # Drawdown
    ax1 = axes[1]
    ax1.fill_between(equity['date'], equity['drawdown'], 0,
                     color='#e74c3c', alpha=0.55)
    ax1.plot(equity['date'], equity['drawdown'], color='#c0392b', linewidth=1)
    ax1.set_ylabel('Drawdown (%)')
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.1f}%'))
    ax1.grid(alpha=0.25)

    # Monthly returns
    ax2 = axes[2]
    colors = ['#2ecc71' if v >= 0 else '#e74c3c' for v in equity['ret_mo'].fillna(0)]
    ax2.bar(equity['date'], equity['ret_mo'].fillna(0), color=colors, alpha=0.8, width=20)
    ax2.axhline(0, color='black', linewidth=0.8)
    ax2.axhline(1.0, color='orange', linestyle='--', linewidth=1, label='1% target')
    ax2.axhline(equity['ret_mo'].mean(), color='steelblue', linestyle='-.',
                linewidth=1, label=f"Avg {equity['ret_mo'].mean():.2f}%")
    ax2.set_ylabel('Monthly Return (%)')
    ax2.set_xlabel('Date')
    ax2.legend(fontsize=8)
    ax2.grid(axis='y', alpha=0.25)

    plt.tight_layout()
    out = Path('backtest_equity.png')
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {out}')


if __name__ == '__main__':
    if not TRADES_CSV.exists() or not EQUITY_CSV.exists():
        print("Run backtest_orats.py first to generate the data files.")
    else:
        trades, equity = load()
        chart_monthly_pnl(trades, equity)
        chart_trade_breakdown(trades)
        chart_equity_drawdown(equity)
        print("\nAll charts saved. Open them with:")
        print("  open backtest_monthly_pnl.png backtest_trade_breakdown.png backtest_equity.png")
