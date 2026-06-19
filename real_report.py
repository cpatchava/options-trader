"""
Real-trade daily report: reads live trades from Google Sheets,
computes per-trader P&L, runs screener, emails each trader.

Sheet structure:
  'Traders' tab  — config: name, email, tab, capital, monthly_target_pct
  One tab per trader — their actual trades (TRADE_COLUMNS in utils/gsheets.py)

Env vars required (in ~/.env or .env):
  GOOGLE_SHEET_ID        — from the sheet URL /d/<ID>/edit
  GMAIL_ADDRESS          — sender Gmail account
  GMAIL_APP_PASSWORD     — Gmail app password

Auth: service account JSON at data/gcp_credentials.json
"""

import warnings
warnings.filterwarnings('ignore')

import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, STOP_LOSS_PCT, TICKER_SECTORS, GOOGLE_SHEET_ID,
)
from screener import screen
from utils.gsheets import get_traders, get_trades, compute_metrics


# ── HTML report builder ────────────────────────────────────────────────────────

def build_trader_report(trader: dict, candidates: list) -> tuple[str, str]:
    """Return (subject, html_body) for a single trader."""
    import pandas as pd
    import yfinance as yf

    name               = trader['name']
    capital            = float(trader.get('capital', 100_000))
    monthly_target_pct = float(trader.get('monthly_target_pct', 0.01))
    tab_name           = trader['tab']
    today              = date.today()

    df      = get_trades(tab_name)
    metrics = compute_metrics(df, capital, monthly_target_pct)

    mtd            = metrics['mtd']
    all_time       = metrics['all_time']
    win_rate       = metrics['win_rate']
    wins           = metrics['wins']
    total_closed   = metrics['total_closed']
    target_monthly = metrics['target_monthly']
    open_puts      = metrics['open_puts']
    open_shares    = metrics['open_shares']
    open_calls     = metrics['open_calls']
    closed         = metrics['closed']
    shares_locked  = metrics['shares_locked']
    puts_locked    = metrics['puts_locked']
    total_locked   = metrics['total_locked']
    capital_free   = metrics['capital_free']
    utilization    = metrics['utilization_pct']

    mtd_pct    = (mtd / target_monthly * 100) if target_monthly else 0
    open_count = len(open_puts) + len(open_shares)
    max_slots  = trader.get('max_positions', None)  # optional limit; None = no cap

    subject = (
        f"Options Report [{name}] — {today.strftime('%a %b %d, %Y')} "
        f"| MTD ${mtd:,.0f} / ${target_monthly:,.0f}"
    )

    # ── HTML skeleton ──────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body        {{ font-family: -apple-system,'Helvetica Neue',Arial,sans-serif;
                 font-size:14px; color:#1a1a1a; max-width:820px; margin:auto; padding:20px; }}
  h2          {{ color:#2c3e50; border-bottom:2px solid #3498db; padding-bottom:6px; }}
  h3          {{ color:#34495e; margin-top:28px; }}
  table       {{ border-collapse:collapse; width:100%; margin:12px 0; }}
  th          {{ background:#2c3e50; color:white; padding:8px 12px; text-align:left; font-size:13px; }}
  td          {{ padding:7px 12px; border-bottom:1px solid #ecf0f1; }}
  tr:hover    {{ background:#f8f9fa; }}
  .metric     {{ display:inline-block; background:#ecf0f1; border-radius:6px;
                 padding:10px 18px; margin:6px; text-align:center; min-width:120px; }}
  .metric-val {{ font-size:20px; font-weight:bold; color:#2980b9; }}
  .metric-lbl {{ font-size:11px; color:#7f8c8d; margin-top:4px; }}
  .green      {{ color:#27ae60; font-weight:bold; }}
  .red        {{ color:#e74c3c; font-weight:bold; }}
  .orange     {{ color:#e67e22; font-weight:bold; }}
  .action     {{ background:#fef9e7; border-left:4px solid #f39c12;
                 padding:10px 14px; margin:8px 0; border-radius:4px; }}
  .action-new {{ background:#eafaf1; border-left-color:#27ae60; }}
  .action-close{{ background:#fdedec; border-left-color:#e74c3c; }}
  footer      {{ margin-top:40px; font-size:11px; color:#aaa; border-top:1px solid #eee; padding-top:10px; }}
</style>
</head>
<body>

<h2>Options Daily Report &nbsp;·&nbsp; {name} &nbsp;·&nbsp; {today.strftime('%B %d, %Y')}</h2>

<p style="margin:4px 0 16px">
  <a href="https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit"
     style="background:#3498db;color:white;padding:7px 16px;border-radius:4px;
            text-decoration:none;font-size:13px;font-weight:bold;">
    &#128196; Open Trade Sheet
  </a>
</p>

<div>
  <div class="metric">
    <div class="metric-val">${mtd:,.0f}</div>
    <div class="metric-lbl">MTD Realized P&amp;L</div>
  </div>
  <div class="metric">
    <div class="metric-val">${target_monthly:,.0f}</div>
    <div class="metric-lbl">Monthly Target ({monthly_target_pct*100:.0f}%)</div>
  </div>
  <div class="metric">
    <div class="metric-val {'green' if mtd_pct >= 100 else 'orange' if mtd_pct >= 50 else 'red'}">{mtd_pct:.0f}%</div>
    <div class="metric-lbl">Target Progress</div>
  </div>
  <div class="metric">
    <div class="metric-val">{len(open_puts)} puts{f" + {len(open_shares)} shares" if not open_shares.empty else ""}</div>
    <div class="metric-lbl">Open Positions{f" ({open_count}/{max_slots} slots)" if max_slots else ""}</div>
  </div>
  <div class="metric">
    <div class="metric-val">${all_time:,.0f}</div>
    <div class="metric-lbl">All-Time P&amp;L</div>
  </div>
  <div class="metric">
    <div class="metric-val">{win_rate}%</div>
    <div class="metric-lbl">Win Rate ({wins}/{total_closed})</div>
  </div>
</div>

<h3>CAPITAL ALLOCATION</h3>
<div>
  <div class="metric">
    <div class="metric-val">${capital:,.0f}</div>
    <div class="metric-lbl">Total Capital</div>
  </div>
  <div class="metric">
    <div class="metric-val red">${shares_locked:,.0f}</div>
    <div class="metric-lbl">Locked in Shares</div>
  </div>
  <div class="metric">
    <div class="metric-val red">${puts_locked:,.0f}</div>
    <div class="metric-lbl">Put Collateral</div>
  </div>
  <div class="metric">
    <div class="metric-val {'green' if capital_free >= 0 else 'red'}">${capital_free:,.0f}</div>
    <div class="metric-lbl">Available Cash</div>
  </div>
  <div class="metric">
    <div class="metric-val {'red' if utilization > 90 else 'orange' if utilization > 70 else 'green'}">{utilization}%</div>
    <div class="metric-lbl">Capital Utilization</div>
  </div>
</div>
"""

    # ── Action items ───────────────────────────────────────────────────────
    actions = _build_actions(open_puts, open_shares, open_calls, candidates, max_slots)
    html += '<h3>ACTION ITEMS</h3>\n'
    if actions:
        for i, a in enumerate(actions, 1):
            css = ('action-close' if a['type'] == 'close'
                   else 'action-new' if a['type'] == 'open'
                   else 'action')
            html += f'<div class="action {css}"><strong>{i}. [{a["type"].upper()}]</strong> {a["description"]}</div>\n'
    else:
        html += '<p><em>No urgent actions today. Monitor open positions.</em></p>\n'

    # ── Held shares ────────────────────────────────────────────────────────
    if not open_shares.empty:
        closed_assigned = closed[closed.get('close_type', '') == 'assigned'] if 'close_type' in closed.columns else closed.iloc[0:0]
        html += '<h3>HELD SHARES</h3>\n'
        html += ('<table><tr><th>Ticker</th><th>Sector</th><th>Shares</th>'
                 '<th>Cost Basis</th><th>Put Premium</th><th>Net Basis</th>'
                 '<th>Current Price</th><th>Net P&amp;L</th><th>Stop Level</th></tr>\n')
        for _, row in open_shares.iterrows():
            tkr      = str(row.get('ticker', ''))
            basis    = float(row.get('strike', 0) or 0)
            n_shares = int(row.get('contracts', 1) or 1)  # actual share count
            sector   = TICKER_SECTORS.get(tkr, 'Other')
            stop     = round(basis * (1 - STOP_LOSS_PCT), 2)

            # find premium from the matched assigned put
            put_match = closed_assigned[closed_assigned['ticker'] == tkr] if not closed_assigned.empty else closed_assigned
            put_pnl   = float(put_match['pnl'].iloc[0]) if not put_match.empty else 0.0
            net_basis = basis - (put_pnl / n_shares) if n_shares else basis

            try:
                cur_px  = float(yf.Ticker(tkr).fast_info.last_price)
                net_pnl = (cur_px - basis) * n_shares + put_pnl
                pnl_cls = 'green' if net_pnl >= 0 else 'red'
                net_pnl_str = f'<span class="{pnl_cls}">${net_pnl:+,.0f}</span>'
                px_str = f'${cur_px:.2f}'
            except Exception:
                net_pnl_str = '—'
                px_str = 'N/A'

            html += (f'<tr><td><b>{tkr}</b></td>'
                     f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
                     f'<td>{n_shares}</td>'
                     f'<td>${basis:.2f}</td>'
                     f'<td class="green">${put_pnl:+,.0f}</td>'
                     f'<td>${net_basis:.2f}</td>'
                     f'<td>{px_str}</td>'
                     f'<td>{net_pnl_str}</td>'
                     f'<td>${stop:.2f}</td></tr>\n')
        html += '</table>\n'

    # ── Open covered calls — live pricing ─────────────────────────────────
    if not open_calls.empty:
        html += '<h3>OPEN COVERED CALLS — Live Pricing</h3>\n'
        html += _live_options_table(open_calls, today, 'call')

    # ── Open puts — live pricing ───────────────────────────────────────────
    if not open_puts.empty:
        html += '<h3>OPEN PUTS — Live Pricing</h3>\n'
        html += _live_options_table(open_puts, today, 'put')

    # ── Roll opportunities — checked for all open puts ─────────────────────
    put_tickers = set(open_puts['ticker'].astype(str).tolist()) if not open_puts.empty else set()

    if not open_puts.empty:
        rolls = _build_roll_analysis(open_puts)
        rolls = [r for r in rolls if r['net_credit'] is None or r['net_credit'] >= 0]
        if rolls:
            html += '<h3>ROLL OPPORTUNITIES</h3>\n'
            html += ('<table><tr>'
                     '<th>Ticker</th>'
                     '<th>Current position</th><th>Close ask</th>'
                     '<th>→ New position</th><th>New bid</th>'
                     '<th>Net/ct</th><th>DTE gain</th><th>Verdict</th>'
                     '</tr>\n')
            for roll in rolls:
                cur_label = f"{roll['cur_expiry'][5:]} ${roll['cur_strike']:.2f}P"
                new_label = f"{roll['new_expiry'][5:]} ${roll['new_strike']:.2f}P"
                ask_str   = f"${roll['cur_ask']:.2f}" if roll['cur_ask'] is not None else 'N/A'
                if roll['net_credit'] is not None:
                    net_str   = f"${roll['net_credit']:+.0f}"
                    net_cls   = 'green' if roll['net_credit'] >= 0 else 'red'
                    verdict, v_cls = _roll_verdict(roll)
                else:
                    net_str = '—'
                    net_cls = ''
                    verdict, v_cls = 'Check live quote', 'orange'
                dte_str = f"+{roll['dte_gain']}d" if roll['dte_gain'] > 0 else f"{roll['dte_gain']}d"
                html += (f'<tr>'
                         f'<td><b>{roll["ticker"]}</b></td>'
                         f'<td style="color:#7f8c8d">{cur_label} ({roll["cur_dte"]}d left)</td>'
                         f'<td>{ask_str}</td>'
                         f'<td>{new_label}</td>'
                         f'<td>${roll["new_bid"]:.2f}</td>'
                         f'<td class="{net_cls}">{net_str}</td>'
                         f'<td>{dte_str}</td>'
                         f'<td class="{v_cls}"><b>{verdict}</b></td>'
                         f'</tr>\n')
            html += '</table>\n'

    # ── New opportunities ──────────────────────────────────────────────────
    html += '<h3>NEW OPPORTUNITIES — Best per Sector</h3>\n'
    if candidates:
        share_tickers = set(open_shares['ticker'].astype(str).tolist()) if not open_shares.empty else set()
        held_tickers  = put_tickers | share_tickers

        # top 3 per sector, excluding tickers already in roll section or held as shares
        sector_counts: dict = {}
        sector_best = []
        for r in candidates:
            if r['ticker'] in held_tickers:
                continue
            s = r.get('sector', 'Other')
            if sector_counts.get(s, 0) < 3:
                sector_counts[s] = sector_counts.get(s, 0) + 1
                sector_best.append(r)

        if sector_best:
            html += ('<table><tr><th>Sector</th><th>Ticker</th>'
                     '<th>Price</th><th>IVR</th><th>IV</th>'
                     '<th>Strike</th><th>Bid</th><th>Delta</th>'
                     '<th>Yield</th><th>Ann Yld</th><th>Expiry</th>'
                     '<th>DTE</th><th>Earnings</th></tr>\n')
            for r in sector_best:
                ivr_color = 'green' if r['iv_rank'] >= 50 else 'orange' if r['iv_rank'] >= 30 else 'red'
                earn_str  = f"{r['earnings_in']}d" if r.get('earnings_in') is not None else '—'
                html += (f'<tr>'
                         f'<td style="color:#7f8c8d;font-size:12px">{r.get("sector","Other")}</td>'
                         f'<td><b>{r["ticker"]}</b></td>'
                         f'<td>${r["price"]:.2f}</td>'
                         f'<td class="{ivr_color}">{r["iv_rank"]:.0f}</td>'
                         f'<td>{r["iv_pct"]}%</td>'
                         f'<td>${r["put_strike"]}</td>'
                         f'<td>${r["put_bid"]}</td>'
                         f'<td>Δ{r["put_delta"]}</td>'
                         f'<td>{r["put_yield_pct"]}%</td>'
                         f'<td>{r["put_ann_yield"]}%</td>'
                         f'<td>{r["expiry"]}</td>'
                         f'<td>{r["dte"]}</td>'
                         f'<td>{earn_str}</td>'
                         f'</tr>\n')
            html += '</table>\n'
        else:
            html += '<p><em>All screener candidates are tickers you already hold.</em></p>\n'
    else:
        html += '<p><em>No screener candidates today (market closed or data unavailable).</em></p>\n'

    html += f"""
<footer>
  Generated {today.isoformat()} &nbsp;·&nbsp; Wheel Strategy &nbsp;·&nbsp;
  Capital ${capital:,.0f} @ {monthly_target_pct*100:.0f}%/mo target &nbsp;·&nbsp;
  Data via yfinance (15-min delay) &nbsp;·&nbsp; Not financial advice
</footer>
</body>
</html>"""

    return subject, html


def _current_option_bid(ticker: str, expiry_str: str, strike: float, option_type: str = 'put'):
    try:
        import yfinance as yf
        chain = yf.Ticker(ticker).option_chain(expiry_str)
        opts  = chain.puts if option_type == 'put' else chain.calls
        row   = opts[abs(opts['strike'] - strike) < 0.01]
        if not row.empty:
            bid = float(row['bid'].iloc[0])
            return bid if bid > 0 else None
    except Exception:
        pass
    return None


def _current_stock_price(ticker: str):
    try:
        import yfinance as yf
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return None


def _live_options_table(df, today, option_type: str) -> str:
    from config import PROFIT_TAKE_PCT
    header = (
        '<table border="1" cellpadding="5" '
        'style="border-collapse:collapse;width:100%;font-size:13px">'
        '<tr style="background:#2c3e50;color:white">'
        '<th>Ticker</th><th>Sector</th><th>Position</th>'
        '<th>Stock (vs strike)</th><th>Opened @</th><th>Current Bid</th>'
        '<th>Decayed</th><th>50% Take @</th><th>Distance</th><th>DTE</th>'
        '</tr>\n'
    )
    rows = ''
    for _, row in df.iterrows():
        tkr       = str(row.get('ticker', ''))
        strike    = float(row.get('strike', 0) or 0)
        open_prem = float(row.get('premium', 0) or 0)
        n         = int(row.get('contracts', 1) or 1)
        exp       = row.get('expiry', '')
        exp_str   = exp.strftime('%Y-%m-%d') if hasattr(exp, 'strftime') else str(exp)[:10]
        sector    = TICKER_SECTORS.get(tkr, 'Other')
        try:
            dte = (date.fromisoformat(exp_str) - today).days
        except Exception:
            dte = '—'

        take_target = round(open_prem * PROFIT_TAKE_PCT, 2)
        cur_stock   = _current_stock_price(tkr)
        cur_bid     = _current_option_bid(tkr, exp_str, strike, option_type)

        stock_str = f'${cur_stock:.2f}' if cur_stock else 'N/A'
        pct_from  = (f'{(cur_stock - strike) / cur_stock * 100:+.1f}%'
                     if cur_stock else '—')

        if cur_bid is not None:
            decayed_pct = (open_prem - cur_bid) / open_prem * 100 if open_prem else 0
            if decayed_pct >= 50:
                decay_color, decay_label = '#27ae60', f'{decayed_pct:.0f}% ✓ TAKE'
            elif decayed_pct >= 30:
                decay_color, decay_label = '#e67e22', f'{decayed_pct:.0f}%'
            else:
                decay_color, decay_label = '#555', f'{decayed_pct:.0f}%'
            distance = cur_bid - take_target
            dist_str = '→ TAKE NOW' if distance <= 0 else f'${distance:.2f} away'
            bid_str  = f'${cur_bid:.2f}'
        else:
            decay_color, decay_label = '#999', 'N/A'
            bid_str, dist_str = 'N/A', '—'

        rows += (
            f'<tr>'
            f'<td><b>{tkr}</b></td>'
            f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
            f'<td>{n}x ${strike:.0f}</td>'
            f'<td>{stock_str} ({pct_from})</td>'
            f'<td>${open_prem:.2f}</td>'
            f'<td>{bid_str}</td>'
            f'<td style="color:{decay_color};font-weight:bold">{decay_label}</td>'
            f'<td>${take_target:.2f}</td>'
            f'<td>{dist_str}</td>'
            f'<td>{dte}d</td>'
            f'</tr>\n'
        )
    return header + rows + '</table>\n'


def _build_roll_analysis(open_puts) -> list:
    """For every open put, find a 21-45 DTE expiry via yfinance and compute roll economics."""
    import yfinance as yf
    import pandas as pd
    from datetime import date, timedelta

    today = date.today()

    rolls = []
    for _, current in open_puts.iterrows():
        tkr = str(current.get('ticker', ''))
        if not tkr:
            continue

        cur_strike    = float(current.get('strike', 0))
        cur_expiry    = current.get('expiry')
        cur_contracts = int(current.get('contracts', 1) or 1)
        cur_premium   = float(current.get('premium', 0) or 0)

        try:
            exp_str = cur_expiry.strftime('%Y-%m-%d') if hasattr(cur_expiry, 'strftime') else str(cur_expiry)[:10]
        except Exception:
            exp_str = ''

        cur_dte = max(0, (pd.Timestamp(exp_str) - pd.Timestamp.today()).days) if exp_str else 0

        # Get current ask to close existing position
        try:
            chain   = yf.Ticker(tkr).option_chain(exp_str)
            match   = chain.puts[abs(chain.puts['strike'] - cur_strike) < 0.01]
            cur_ask = float(match['ask'].iloc[0]) if not match.empty else None
        except Exception:
            cur_ask = None

        # Find nearest expiry in the 21-45 DTE window
        try:
            ticker_obj  = yf.Ticker(tkr)
            expirations = ticker_obj.options

            target_exp = None
            target_dte = None
            for exp in expirations:
                try:
                    exp_date = date.fromisoformat(exp)
                    dte = (exp_date - today).days
                    if 21 <= dte <= 45:
                        if target_exp is None or dte < target_dte:
                            target_exp = exp
                            target_dte = dte
                except Exception:
                    continue

            if target_exp is None:
                continue

            # Find nearest strike in the new chain
            new_chain = ticker_obj.option_chain(target_exp)
            puts = new_chain.puts.copy()
            if puts.empty:
                continue

            puts['_diff'] = abs(puts['strike'] - cur_strike)
            best       = puts.nsmallest(1, '_diff').iloc[0]
            new_strike = float(best['strike'])
            new_bid    = float(best['bid'])
            if new_bid <= 0:
                new_bid = float(best.get('ask', 0) or 0) / 2

        except Exception:
            continue

        net_credit = round((new_bid - cur_ask) * 100 * cur_contracts, 2) if cur_ask is not None else None

        rolls.append({
            'ticker':        tkr,
            'cur_strike':    cur_strike,
            'cur_expiry':    exp_str,
            'cur_dte':       cur_dte,
            'cur_premium':   cur_premium,
            'cur_ask':       cur_ask,
            'new_strike':    new_strike,
            'new_expiry':    target_exp,
            'new_dte':       target_dte,
            'new_bid':       new_bid,
            'contracts':     cur_contracts,
            'net_credit':    net_credit,
            'dte_gain':      target_dte - cur_dte,
            'strike_change': new_strike - cur_strike,
        })

    return rolls


def _roll_verdict(roll: dict) -> tuple[str, str]:
    """Return (verdict text, css class) for a roll."""
    net = roll['net_credit']
    dte_gain = roll['dte_gain']
    strike_ch = roll['strike_change']

    if net is None:
        return 'Check live quote', 'orange'

    if net >= 0 and dte_gain > 0:
        extra = f" + lower strike ${roll['new_strike']:.0f}" if strike_ch < 0 else ''
        return f'Roll — free extension{extra}', 'green'
    elif net >= 0 and strike_ch < 0:
        return f'Roll down ${abs(strike_ch):.2f} for net credit', 'green'
    elif net < 0 and dte_gain >= 14 and abs(net) < 50 * roll['contracts']:
        return f'Roll costs ${abs(net):.0f} — reasonable for +{dte_gain}d', 'orange'
    elif net < 0:
        return f'Skip — costs ${abs(net):.0f} to roll', 'red'
    else:
        return 'Evaluate', 'orange'


def _notes(row) -> str:
    """Return notes string, converting NaN to empty."""
    import math
    v = row.get('notes', '')
    if v is None:
        return ''
    try:
        if math.isnan(float(v)):
            return ''
    except (TypeError, ValueError):
        pass
    return str(v)


def _build_actions(open_puts, open_shares, open_calls, candidates, max_slots) -> list:
    """Generate action items from real trade data."""
    import yfinance as yf
    actions = []
    today = date.today()

    # ── Stop-loss alerts for held shares ──────────────────────────────────
    if not open_shares.empty:
        for _, row in open_shares.iterrows():
            tkr   = str(row.get('ticker', ''))
            basis = float(row.get('strike', 0) or 0)
            if not basis or not tkr:
                continue
            stop = round(basis * (1 - STOP_LOSS_PCT), 2)
            n    = int(row.get('contracts', 1) or 1)  # actual share count
            try:
                cur_px        = float(yf.Ticker(tkr).fast_info.last_price)
                pct_from_stop = (cur_px - stop) / basis * 100
                if cur_px <= stop:
                    actions.append({
                        'type': 'close',
                        'description': (
                            f"🚨 <b>STOP TRIGGERED — {tkr}</b>: "
                            f"current ${cur_px:.2f} ≤ stop ${stop:.2f} "
                            f"(basis ${basis:.2f}, −{STOP_LOSS_PCT*100:.0f}%). "
                            f"<b>SELL {n} shares NOW. Log close_date + close_price in the sheet.</b>"
                        ),
                    })
                elif pct_from_stop < 5:
                    actions.append({
                        'type': 'monitor',
                        'description': (
                            f"⚠️ <b>Stop warning — {tkr}</b>: "
                            f"${cur_px:.2f} current, stop at ${stop:.2f} "
                            f"({pct_from_stop:.1f}% above trigger). "
                            f"Confirm GTC stop order is live on Schwab."
                        ),
                    })
            except Exception:
                pass

    # ── Expiry alerts ──────────────────────────────────────────────────────
    for df_part in [open_puts, open_calls]:
        if df_part.empty:
            continue
        for _, row in df_part.iterrows():
            try:
                exp = row.get('expiry')
                if exp is None:
                    continue
                exp_date = exp.date() if hasattr(exp, 'date') else date.fromisoformat(str(exp)[:10])
                dte      = (exp_date - today).days
                exp_str  = exp_date.strftime('%Y-%m-%d')
                opt_type = str(row.get('type', 'PUT')).upper()
                tkr      = str(row.get('ticker', ''))
                strike   = float(row.get('strike', 0))
                if dte <= 2:
                    actions.append({
                        'type': 'close',
                        'description': (
                            f"<b>ACTION REQUIRED</b> — {tkr} {opt_type} ${strike:.2f} "
                            f"expires {exp_str} ({dte}d). "
                            f"Check assignment status on Schwab. "
                            f"Update close_date + close_price in sheet."
                        ),
                    })
                elif dte <= 7:
                    actions.append({
                        'type': 'monitor',
                        'description': (
                            f"{tkr} {opt_type} ${strike:.2f} expires {exp_str} ({dte} DTE) — "
                            f"verify status on Schwab."
                        ),
                    })
            except Exception:
                pass

    # ── New opening opportunities ──────────────────────────────────────────
    open_tickers = set()
    if not open_puts.empty:
        open_tickers |= set(open_puts['ticker'].astype(str).tolist())
    if not open_shares.empty:
        open_tickers |= set(open_shares['ticker'].astype(str).tolist())

    # Always show top opportunities — no hard slot cap on real portfolio
    if candidates:
        shown = 0
        for r in candidates[:5]:
            if r['ticker'] not in open_tickers and shown < 3:
                actions.append({
                    'type': 'open',
                    'description': (
                        f"<b>{r['ticker']}</b> — Sell {r['expiry']} "
                        f"<b>${r['put_strike']} Put</b> @ ${r['put_bid']} bid "
                        f"({r['put_yield_pct']}% / {r['put_ann_yield']}% ann, "
                        f"Δ{r['put_delta']}, IVR {r['iv_rank']:.0f}). "
                        f"Verify quote on Schwab. "
                        f"<b>Add row to your sheet with status=open.</b>"
                    ),
                })
                shown += 1

    return actions


# ── Email sender ───────────────────────────────────────────────────────────────

def send_report(trader: dict, subject: str, html_body: str):
    """Send the report to one or more comma-separated emails in the trader's sheet row."""
    raw = trader.get('email', '') or ''
    recipients = [e.strip() for e in raw.split(',') if e.strip()]
    if not recipients:
        print(f"  [SKIP] {trader['name']}: no email address in sheet.")
        return

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print(f"  [EMAIL] No Gmail credentials in .env — printing subject only.\n  {subject}")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    print(f"  Report emailed to {', '.join(recipients)} ({trader['name']})")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from datetime import date

    print(f"Real-trade report — {date.today()}")
    print("Running screener (shared across all traders)...")
    try:
        candidates = screen()
        print(f"  Screener: {len(candidates)} candidates")
    except Exception as e:
        print(f"  Screener failed: {e}")
        candidates = []

    print("Reading traders from Google Sheets...")
    try:
        traders = get_traders()
    except Exception as e:
        print(f"  ERROR: could not read Traders tab: {e}")
        sys.exit(1)

    if not traders:
        print("  No traders found in sheet. Exiting.")
        sys.exit(0)

    print(f"  {len(traders)} trader(s): {', '.join(t['name'] for t in traders)}")

    for trader in traders:
        print(f"\nProcessing {trader['name']} (tab: {trader['tab']})...")
        try:
            subject, html = build_trader_report(trader, candidates)
            send_report(trader, subject, html)
        except Exception as e:
            print(f"  ERROR for {trader['name']}: {e}")
            import traceback
            traceback.print_exc()

    print("\nDone.")
