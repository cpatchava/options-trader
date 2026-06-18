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
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, STOP_LOSS_PCT, TICKER_SECTORS,
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

    # ── Open covered calls ─────────────────────────────────────────────────
    if not open_calls.empty:
        html += '<h3>OPEN COVERED CALLS</h3>\n'
        html += ('<table><tr><th>Ticker</th><th>Sector</th><th>Strike</th>'
                 '<th>Expiry</th><th>Premium</th><th>Contracts</th>'
                 '<th>Notes</th></tr>\n')
        for _, row in open_calls.iterrows():
            tkr    = str(row.get('ticker', ''))
            sector = TICKER_SECTORS.get(tkr, 'Other')
            exp    = row.get('expiry', '')
            exp_str = exp.strftime('%Y-%m-%d') if hasattr(exp, 'strftime') else str(exp)[:10]
            html += (f'<tr><td><b>{tkr}</b></td>'
                     f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
                     f'<td>${float(row.get("strike",0)):.2f}</td>'
                     f'<td>{exp_str}</td>'
                     f'<td>${float(row.get("premium",0)):.2f}</td>'
                     f'<td>{int(row.get("contracts",1))}</td>'
                     f'<td>{_notes(row)}</td></tr>\n')
        html += '</table>\n'

    # ── Open puts ──────────────────────────────────────────────────────────
    if not open_puts.empty:
        html += '<h3>OPEN PUTS</h3>\n'
        html += ('<table><tr><th>Ticker</th><th>Sector</th><th>Strike</th>'
                 '<th>Expiry</th><th>Premium</th><th>Contracts</th>'
                 '<th>Notes</th></tr>\n')
        for _, row in open_puts.iterrows():
            tkr    = str(row.get('ticker', ''))
            sector = TICKER_SECTORS.get(tkr, 'Other')
            exp    = row.get('expiry', '')
            exp_str = exp.strftime('%Y-%m-%d') if hasattr(exp, 'strftime') else str(exp)[:10]
            html += (f'<tr><td><b>{tkr}</b></td>'
                     f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
                     f'<td>${float(row.get("strike",0)):.2f}</td>'
                     f'<td>{exp_str}</td>'
                     f'<td>${float(row.get("premium",0)):.2f}</td>'
                     f'<td>{int(row.get("contracts",1))}</td>'
                     f'<td>{_notes(row)}</td></tr>\n')
        html += '</table>\n'

    # ── New opportunities ──────────────────────────────────────────────────
    html += '<h3>NEW OPPORTUNITIES — Best per Sector</h3>\n'
    if candidates:
        open_tickers = set()
        if not open_puts.empty:
            open_tickers |= set(open_puts['ticker'].astype(str).tolist())
        if not open_shares.empty:
            open_tickers |= set(open_shares['ticker'].astype(str).tolist())

        seen_sectors: set = set()
        sector_best = []
        for r in candidates:
            s = r.get('sector', 'Other')
            if s not in seen_sectors:
                seen_sectors.add(s)
                sector_best.append(r)

        html += ('<table><tr><th>Sector</th><th>Ticker</th><th>Held?</th>'
                 '<th>Price</th><th>IVR</th><th>IV</th>'
                 '<th>Strike</th><th>Bid</th><th>Delta</th>'
                 '<th>Yield</th><th>Ann Yld</th><th>Expiry</th>'
                 '<th>DTE</th><th>Earnings</th></tr>\n')
        for r in sector_best:
            ivr_color  = 'green' if r['iv_rank'] >= 50 else 'orange' if r['iv_rank'] >= 30 else 'red'
            earn_str   = f"{r['earnings_in']}d" if r.get('earnings_in') is not None else '—'
            held       = '✓ open' if r['ticker'] in open_tickers else '—'
            held_style = 'color:#e67e22;font-weight:bold' if held != '—' else 'color:#7f8c8d'
            html += (f'<tr>'
                     f'<td style="color:#7f8c8d;font-size:12px">{r.get("sector","Other")}</td>'
                     f'<td><b>{r["ticker"]}</b></td>'
                     f'<td style="{held_style}">{held}</td>'
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
        html += '<p><em>No screener candidates today (market closed or data unavailable).</em></p>\n'

    # ── Closed trade history (last 10) ─────────────────────────────────────
    if not closed.empty:
        recent = closed.sort_values('close_date', ascending=False).head(10)
        html += '<h3>RECENT CLOSED TRADES (last 10)</h3>\n'
        html += ('<table><tr><th>Ticker</th><th>Type</th><th>Strike</th>'
                 '<th>Opened</th><th>Closed</th><th>Premium</th>'
                 '<th>Close Price</th><th>P&amp;L</th></tr>\n')
        for _, row in recent.iterrows():
            pnl     = float(row.get('pnl', 0) or 0)
            pnl_cls = 'green' if pnl >= 0 else 'red'
            o_date  = row.get('open_date', '')
            c_date  = row.get('close_date', '')
            o_str   = o_date.strftime('%Y-%m-%d') if hasattr(o_date, 'strftime') else str(o_date)[:10]
            c_str   = c_date.strftime('%Y-%m-%d') if hasattr(c_date, 'strftime') else str(c_date)[:10]
            html += (f'<tr><td><b>{row.get("ticker","")}</b></td>'
                     f'<td>{str(row.get("type","")).upper()}</td>'
                     f'<td>${float(row.get("strike",0)):.2f}</td>'
                     f'<td>{o_str}</td>'
                     f'<td>{c_str}</td>'
                     f'<td>${float(row.get("premium",0)):.2f}</td>'
                     f'<td>${float(row.get("close_price",0) or 0):.2f}</td>'
                     f'<td class="{pnl_cls}">${pnl:+,.0f}</td></tr>\n')
        html += '</table>\n'

    html += f"""
<footer>
  Generated {today.isoformat()} &nbsp;·&nbsp; Wheel Strategy &nbsp;·&nbsp;
  Capital ${capital:,.0f} @ {monthly_target_pct*100:.0f}%/mo target &nbsp;·&nbsp;
  Data via yfinance (15-min delay) &nbsp;·&nbsp; Not financial advice
</footer>
</body>
</html>"""

    return subject, html


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
    """Send the report to the trader's email address."""
    recipient = trader.get('email', '')
    if not recipient:
        print(f"  [SKIP] {trader['name']}: no email address in sheet.")
        return

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print(f"  [EMAIL] No Gmail credentials in .env — printing subject only.\n  {subject}")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = recipient
    msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_ADDRESS, recipient, msg.as_string())

    print(f"  Report emailed to {recipient} ({trader['name']})")


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
