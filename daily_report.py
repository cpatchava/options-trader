"""
Daily options report: runs screener, checks open positions, emails results.
Scheduled via cron to run each weekday at 8 AM.
"""

import warnings
warnings.filterwarnings('ignore')

import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    STARTING_CAPITAL, TARGET_MONTHLY_RETURN, EMAIL_RECIPIENT,
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, STOP_LOSS_PCT, MAX_POSITIONS, TICKER_SECTORS,
)
from screener import screen
from portfolio import load_trades, open_positions, assigned_shares
from paper_trading import _load as _load_paper_trades, build_live_positions_html


# ── Report generation ──────────────────────────────────────────────────────────

def build_report() -> tuple[str, str]:
    """Return (subject, html_body)."""
    import pandas as pd
    today = date.today()
    candidates = screen()
    df = load_trades()
    open_pos = open_positions(df)
    share_pos = assigned_shares(df)

    # ── Paper trading metrics (source of truth) ────────────────────────────
    paper_df     = _load_paper_trades()
    paper_open   = paper_df[(paper_df['status'] == 'open') & (paper_df['option_type'] == 'put')]
    paper_shares = paper_df[(paper_df['status'] == 'open') & (paper_df['option_type'] == 'shares')]
    paper_closed = paper_df[paper_df['status'] == 'closed']
    month_start  = pd.Timestamp(today.replace(day=1))
    mtd = paper_df[(paper_df['status'] == 'closed') &
                   (pd.to_datetime(paper_df['close_date']) >= month_start)]['pnl'].sum()
    mtd        = float(mtd) if not pd.isna(mtd) else 0
    total_cl   = len(paper_closed)
    wins       = int((paper_closed['pnl'] > 0).sum()) if not paper_closed.empty else 0
    win_rate   = round(wins / total_cl * 100, 1) if total_cl > 0 else 0.0
    open_count = len(paper_open) + len(paper_shares)  # shares occupy a slot

    target_monthly = STARTING_CAPITAL * TARGET_MONTHLY_RETURN
    mtd_pct = (mtd / target_monthly * 100) if target_monthly else 0

    subject = f"Options Report — {today.strftime('%a %b %d, %Y')} | MTD ${mtd:,.0f} / ${target_monthly:,.0f}"

    # ── Action items ───────────────────────────────────────────────────────
    actions = _build_actions(open_pos, candidates, share_pos, paper_open, paper_shares)

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body        {{ font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
                 font-size: 14px; color: #1a1a1a; max-width: 780px; margin: auto; padding: 20px; }}
  h2          {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 6px; }}
  h3          {{ color: #34495e; margin-top: 28px; }}
  table       {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th          {{ background: #2c3e50; color: white; padding: 8px 12px; text-align: left; font-size: 13px; }}
  td          {{ padding: 7px 12px; border-bottom: 1px solid #ecf0f1; }}
  tr:hover    {{ background: #f8f9fa; }}
  .metric     {{ display: inline-block; background: #ecf0f1; border-radius: 6px;
                 padding: 10px 18px; margin: 6px; text-align: center; min-width: 120px; }}
  .metric-val {{ font-size: 20px; font-weight: bold; color: #2980b9; }}
  .metric-lbl {{ font-size: 11px; color: #7f8c8d; margin-top: 4px; }}
  .green      {{ color: #27ae60; font-weight: bold; }}
  .red        {{ color: #e74c3c; font-weight: bold; }}
  .orange     {{ color: #e67e22; font-weight: bold; }}
  .action     {{ background: #fef9e7; border-left: 4px solid #f39c12;
                 padding: 10px 14px; margin: 8px 0; border-radius: 4px; }}
  .action-new {{ background: #eafaf1; border-left-color: #27ae60; }}
  .action-close{{ background: #fdedec; border-left-color: #e74c3c; }}
  footer      {{ margin-top: 40px; font-size: 11px; color: #aaa; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>

<h2>Options Daily Report &nbsp;·&nbsp; {today.strftime('%B %d, %Y')}</h2>

<div>
  <div class="metric">
    <div class="metric-val">${mtd:,.0f}</div>
    <div class="metric-lbl">MTD Realized P&L</div>
  </div>
  <div class="metric">
    <div class="metric-val">${target_monthly:,.0f}</div>
    <div class="metric-lbl">Monthly Target (1%)</div>
  </div>
  <div class="metric">
    <div class="metric-val {'green' if mtd_pct >= 100 else 'orange' if mtd_pct >= 50 else 'red'}">{mtd_pct:.0f}%</div>
    <div class="metric-lbl">Target Progress</div>
  </div>
  <div class="metric">
    <div class="metric-val">{open_count}</div>
    <div class="metric-lbl">Open Puts</div>
  </div>
  <div class="metric">
    <div class="metric-val">{win_rate}%</div>
    <div class="metric-lbl">Win Rate ({wins}/{total_cl})</div>
  </div>
</div>

<h3>ACTION ITEMS</h3>
"""

    if actions:
        for i, a in enumerate(actions, 1):
            css = 'action-close' if a['type'] == 'close' else 'action-new' if a['type'] == 'open' else 'action'
            html += f'<div class="action {css}"><strong>{i}. [{a["type"].upper()}]</strong> {a["description"]}</div>\n'
    else:
        html += '<p><em>No urgent actions today. Monitor open positions.</em></p>'

    # ── Open positions ─────────────────────────────────────────────────────
    html += '<h3>OPEN POSITIONS</h3>'
    if not open_pos.empty:
        from portfolio import COMMISSION
        html += """<table>
<tr><th>Ticker</th><th>Type</th><th>Strike</th><th>Expiry</th>
    <th>Premium</th><th>Contracts</th><th>Pred. P&L</th><th>Opened</th></tr>"""
        for _, row in open_pos.iterrows():
            pred = (row['open_premium'] * 100 * row['contracts']
                    - COMMISSION * row['contracts'] * 2)
            exp_str = row['expiry'].strftime('%Y-%m-%d') if hasattr(row['expiry'], 'strftime') else str(row['expiry'])
            html += (f"<tr><td><b>{row['ticker']}</b></td>"
                     f"<td>{str(row['option_type']).upper()}</td>"
                     f"<td>${row['strike']:.2f}</td>"
                     f"<td>{exp_str}</td>"
                     f"<td>${row['open_premium']:.2f}</td>"
                     f"<td>{row['contracts']}</td>"
                     f"<td class='green'>${pred:.0f}</td>"
                     f"<td>{str(row['open_date'])[:10]}</td></tr>\n")
        html += '</table>'
    else:
        html += '<p><em>No open positions.</em></p>'

    # ── Held shares ────────────────────────────────────────────────────────
    if not paper_shares.empty:
        import yfinance as yf
        html += '<h3>HELD SHARES (Pending Covered Call)</h3>'
        html += ('<table><tr><th>Ticker</th><th>Sector</th><th>Shares</th>'
                 '<th>Cost Basis</th><th>Current Price</th><th>Unrealized P&L</th>'
                 '<th>Stop Level</th><th>CC Eligible</th></tr>')
        for _, row in paper_shares.iterrows():
            tkr       = row['ticker']
            basis     = float(row['strike'])
            n_shares  = int(row['contracts']) * 100
            sector    = TICKER_SECTORS.get(tkr, 'Other')
            stop      = round(basis * (1 - STOP_LOSS_PCT), 2)
            try:
                cur_px = float(yf.Ticker(tkr).fast_info.last_price)
                unreal = (cur_px - basis) * n_shares
                unreal_str = f'<span class="{"green" if unreal >= 0 else "red"}">${unreal:+,.0f}</span>'
                px_str = f'${cur_px:.2f}'
                cc_ok  = cur_px >= basis
                cc_str = ('<span class="green">Yes — write call ≥ $' + f'{basis:.0f}</span>'
                          if cc_ok else f'No — ${basis - cur_px:.2f} below basis')
            except Exception:
                unreal_str = '—'
                px_str     = 'N/A'
                cc_str     = '—'
            html += (f'<tr><td><b>{tkr}</b></td>'
                     f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
                     f'<td>{n_shares}</td>'
                     f'<td>${basis:.2f}</td>'
                     f'<td>{px_str}</td>'
                     f'<td>{unreal_str}</td>'
                     f'<td>${stop:.2f}</td>'
                     f'<td>{cc_str}</td></tr>\n')
        html += '</table>'

    # ── Live puts ──────────────────────────────────────────────────────────
    paper_puts = paper_df[(paper_df['status'] == 'open') & (paper_df['option_type'] == 'put')]
    html += '<h3>OPEN PAPER PUTS — Live Pricing</h3>'
    html += build_live_positions_html(paper_puts, today)

    # ── New opportunities — best per sector ───────────────────────────────
    html += '<h3>NEW OPPORTUNITIES — Best per Sector</h3>'
    if candidates:
        # candidates is already sorted by score desc; pick best per sector
        seen_sectors: set = set()
        sector_best = []
        for r in candidates:
            s = r.get('sector', 'Other')
            if s not in seen_sectors:
                seen_sectors.add(s)
                sector_best.append(r)

        paper_held = set(paper_open['ticker'].tolist()) if not paper_open.empty else set()
        html += """<table>
<tr><th>Sector</th><th>Ticker</th><th>Held?</th><th>Price</th><th>IVR</th><th>IV</th>
    <th>Strike</th><th>Bid</th><th>Delta</th><th>Yield</th><th>Ann Yld</th>
    <th>Expiry</th><th>DTE</th><th>Earnings</th></tr>"""
        for r in sector_best:
            ivr_color = 'green' if r['iv_rank'] >= 50 else 'orange' if r['iv_rank'] >= 30 else 'red'
            earn_str  = f"{r['earnings_in']}d" if r.get('earnings_in') is not None else '—'
            held      = '✓ open' if r['ticker'] in paper_held else '—'
            held_style = 'color:#7f8c8d' if held == '—' else 'color:#e67e22;font-weight:bold'
            html += (f"<tr>"
                     f"<td style='color:#7f8c8d;font-size:12px'>{r.get('sector','Other')}</td>"
                     f"<td><b>{r['ticker']}</b></td>"
                     f"<td style='{held_style}'>{held}</td>"
                     f"<td>${r['price']:.2f}</td>"
                     f"<td class='{ivr_color}'>{r['iv_rank']:.0f}</td>"
                     f"<td>{r['iv_pct']}%</td>"
                     f"<td>${r['put_strike']}</td>"
                     f"<td>${r['put_bid']}</td>"
                     f"<td>Δ{r['put_delta']}</td>"
                     f"<td>{r['put_yield_pct']}%</td>"
                     f"<td>{r['put_ann_yield']}%</td>"
                     f"<td>{r['expiry']}</td>"
                     f"<td>{r['dte']}</td>"
                     f"<td>{earn_str}</td>"
                     f"</tr>\n")
        html += '</table>'

    # ── All-time paper P&L ─────────────────────────────────────────────────
    if total_cl > 0:
        total_collected = float(paper_closed['pnl'].sum()) if not paper_closed.empty else 0
        html += f"""<h3>ALL-TIME P&amp;L (Paper)</h3>
<p>Total premium collected: <b>${total_collected:,.0f}</b> &nbsp;·&nbsp;
   Closed trades: <b>{total_cl}</b> &nbsp;·&nbsp;
   Win rate: <b>{win_rate}%</b></p>"""

    html += f"""
<footer>
  Generated {today.isoformat()} · Wheel Strategy · Target $100K @ 1%/mo · Data via yfinance (15-min delay) · Not financial advice
</footer>
</body>
</html>"""

    return subject, html, candidates


def _build_actions(open_pos_df, candidates, share_pos_df=None, paper_open_df=None, paper_shares_df=None) -> list:
    import yfinance as yf
    actions = []
    today = date.today()

    # ── Stop-loss alerts for held shares ──────────────────────────────────
    if share_pos_df is not None and not share_pos_df.empty:
        for _, row in share_pos_df.iterrows():
            try:
                cost_basis = float(row['strike'])
                stop_level = round(cost_basis * (1 - STOP_LOSS_PCT), 2)
                ticker     = row['ticker']
                cur_px     = float(yf.Ticker(ticker).fast_info.last_price)
                pct_from_stop = (cur_px - stop_level) / cost_basis * 100

                if cur_px <= stop_level:
                    actions.append({
                        'type': 'close',
                        'description': (
                            f"🚨 <b>STOP TRIGGERED — {ticker}</b>: "
                            f"current ${cur_px:.2f} ≤ stop ${stop_level:.2f} "
                            f"(cost basis ${cost_basis:.2f}, −{STOP_LOSS_PCT*100:.0f}%). "
                            f"<b>SELL {int(row['contracts'])*100} shares on Schwab NOW. "
                            f"Log close in trades.csv.</b>"
                        ),
                    })
                elif pct_from_stop < 5:
                    actions.append({
                        'type': 'monitor',
                        'description': (
                            f"⚠️ <b>Stop warning — {ticker}</b>: "
                            f"current ${cur_px:.2f}, stop at ${stop_level:.2f} "
                            f"(only {pct_from_stop:.1f}% above trigger). "
                            f"Confirm GTC stop order is live on Schwab."
                        ),
                    })
                else:
                    # Normal: show recovery progress
                    pct_to_basis = (cur_px / cost_basis - 1) * 100
                    actions.append({
                        'type': 'monitor',
                        'description': (
                            f"📊 Holding {ticker} shares: "
                            f"current ${cur_px:.2f} vs cost basis ${cost_basis:.2f} "
                            f"({pct_to_basis:+.1f}%). "
                            f"Stop at ${stop_level:.2f}. "
                            f"{'<b>Write covered call — stock recovered to basis.</b>' if cur_px >= cost_basis else 'Waiting for recovery before writing covered call.'}"
                        ),
                    })
            except Exception:
                pass

    # ── Expiry alerts for open options ────────────────────────────────────
    if not open_pos_df.empty:
        opts = open_pos_df[open_pos_df['option_type'].str.lower().isin(['put', 'call'])]
        for _, row in opts.iterrows():
            try:
                exp = row['expiry'].date() if hasattr(row['expiry'], 'date') else date.fromisoformat(str(row['expiry'])[:10])
                dte = (exp - today).days
                exp_str = exp.strftime('%Y-%m-%d')
                if dte <= 2:
                    actions.append({
                        'type': 'close',
                        'description': (
                            f"<b>ACTION REQUIRED</b> — {row['ticker']} "
                            f"{str(row['option_type']).upper()} ${row['strike']} expires {exp_str} ({dte}d). "
                            f"Check if assigned or expired. Update trades.csv."
                        ),
                    })
                elif dte <= 7:
                    actions.append({
                        'type': 'monitor',
                        'description': (
                            f"{row['ticker']} {str(row['option_type']).upper()} ${row['strike']} "
                            f"expires {exp_str} ({dte} DTE) — verify status on Schwab."
                        ),
                    })
            except Exception:
                pass

    # ── New opening opportunities ─────────────────────────────────────────
    paper_tickers = (set(paper_open_df['ticker'].tolist())
                     if paper_open_df is not None and not paper_open_df.empty else set())
    share_tickers = (set(paper_shares_df['ticker'].tolist())
                     if paper_shares_df is not None and not paper_shares_df.empty else set())
    slots_free = MAX_POSITIONS - len(paper_tickers) - len(share_tickers)
    if slots_free > 0:
        for r in candidates[:3]:
            if r['ticker'] not in paper_tickers:
                actions.append({
                    'type': 'open',
                    'description': (
                        f"<b>{r['ticker']}</b> — Sell {r['expiry']} "
                        f"<b>${r['put_strike']} Put</b> @ ${r['put_bid']} bid "
                        f"({r['put_yield_pct']}% / {r['put_ann_yield']}% ann, "
                        f"Δ{r['put_delta']}, IVR {r['iv_rank']:.0f}). "
                        f"Verify quote on Schwab. Add row to trades.csv with your fill."
                    ),
                })
    else:
        actions.append({
            'type': 'monitor',
            'description': (
                f"All {MAX_POSITIONS} slots filled — no new positions until one closes. "
                f"Top screener candidate if a slot opens: <b>{candidates[0]['ticker']}</b> "
                f"${candidates[0]['put_strike']} Put @ ${candidates[0]['put_bid']} "
                f"(IVR {candidates[0]['iv_rank']:.0f})."
            ) if candidates else f"All {MAX_POSITIONS} slots filled.",
        })

    return actions


# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, candidates: list | None = None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  [EMAIL] No credentials found in .env — printing report instead.\n")
        print(f"SUBJECT: {subject}\n")
        _print_plain_summary(candidates)
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

    print(f"  Report emailed to {EMAIL_RECIPIENT}")


def _print_plain_summary(candidates: list | None = None):
    """Fallback plain-text print when no email credentials."""
    import pandas as pd
    today = date.today()
    if candidates is None:
        candidates = screen()
    target = STARTING_CAPITAL * TARGET_MONTHLY_RETURN
    paper_df     = _load_paper_trades()
    paper_open   = paper_df[(paper_df['status'] == 'open') & (paper_df['option_type'] == 'put')]
    paper_closed = paper_df[paper_df['status'] == 'closed']
    month_start  = pd.Timestamp(today.replace(day=1))
    mtd = paper_df[(paper_df['status'] == 'closed') &
                   (pd.to_datetime(paper_df['close_date']) >= month_start)]['pnl'].sum()
    mtd = float(mtd) if not pd.isna(mtd) else 0
    total_cl = len(paper_closed)
    wins = int((paper_closed['pnl'] > 0).sum()) if not paper_closed.empty else 0
    win_rate = round(wins / total_cl * 100, 1) if total_cl > 0 else 0.0

    print("=" * 60)
    print(f"OPTIONS REPORT — {today.strftime('%a %b %d, %Y')}")
    print("=" * 60)
    print(f"  MTD Collected : ${mtd:,.0f} / ${target:,.0f} target")
    print(f"  Open Positions: {len(paper_open)}")
    print(f"  Win Rate      : {win_rate}%")
    print("\nTOP OPPORTUNITIES:")
    for i, r in enumerate(candidates[:5], 1):
        print(f"  {i}. {r['ticker']:6} IVR={r['iv_rank']:.0f}  IV={r['iv_pct']}%  "
              f"${r['put_strike']} Put @ ${r['put_bid']} Δ{r['put_delta']} ({r['put_yield_pct']}%)  "
              f"Ann {r['put_ann_yield']}%/yr  Exp {r['expiry']} ({r['dte']}d)")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"Generating report for {date.today()}...")
    subject, html, candidates = build_report()
    send_email(subject, html, candidates)
    print("Done.")
