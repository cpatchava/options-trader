"""
Paper trading engine — runs daily at market close (set up via cron).

What it does each day:
  1. Manages open paper positions:
       - 50% profit take: close if current bid ≤ 50% of open premium
       - Expiry: mark expired OTM (full profit) or assigned (hold shares)
       - Stop loss: if holding shares that dropped >20% below cost basis → stop
  2. Opens new positions from today's screener, up to MAX_POSITIONS slots
  3. Appends a daily snapshot to data/paper_log.csv for performance tracking
  4. Prints a summary to stdout (captured in cron log)

Paper trades stored in: data/paper_trades.csv
Daily P&L log stored in: data/paper_log.csv
"""

import warnings
warnings.filterwarnings('ignore')

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from config import (
    STARTING_CAPITAL, POSITION_SIZE_PCT, MAX_POSITIONS,
    STOP_LOSS_PCT, EMAIL_RECIPIENT, GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
)
from screener import screen

PAPER_TRADES_FILE = Path('data/paper_trades.csv')
PAPER_LOG_FILE    = Path('data/paper_log.csv')
PROFIT_TAKE_PCT   = 0.50   # close when bid ≤ 50% of open premium
COMMISSION        = 0.65   # per contract per leg

COLS = [
    'open_date', 'ticker', 'option_type', 'strike', 'expiry',
    'contracts', 'open_premium', 'contract_symbol',
    'close_date', 'close_premium', 'close_type', 'pnl', 'status',
]


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load() -> pd.DataFrame:
    str_cols = ['ticker', 'option_type', 'contract_symbol', 'close_type', 'status']
    if PAPER_TRADES_FILE.exists():
        df = pd.read_csv(PAPER_TRADES_FILE, parse_dates=['open_date', 'close_date'])
        df['expiry'] = pd.to_datetime(df['expiry'])
    else:
        df = pd.DataFrame(columns=COLS)
    for c in str_cols:
        if c in df.columns:
            df[c] = df[c].astype(object)
    return df


def _save(df: pd.DataFrame):
    df.to_csv(PAPER_TRADES_FILE, index=False)


def _load_log() -> pd.DataFrame:
    if PAPER_LOG_FILE.exists():
        return pd.read_csv(PAPER_LOG_FILE, parse_dates=['date'])
    return pd.DataFrame(columns=['date', 'portfolio_value', 'open_positions',
                                  'mtd_realized', 'total_realized'])


def _save_log(log: pd.DataFrame):
    log.to_csv(PAPER_LOG_FILE, index=False)


# ── Option price lookup ───────────────────────────────────────────────────────

def _current_option_bid(ticker: str, expiry_str: str, strike: float,
                        option_type: str = 'put') -> float | None:
    """Fetch current bid for a specific option contract via yfinance."""
    try:
        t = yf.Ticker(ticker)
        chain = t.option_chain(expiry_str)
        opts = chain.puts if option_type == 'put' else chain.calls
        row = opts[opts['strike'] == strike]
        if not row.empty:
            bid = float(row['bid'].iloc[0])
            return bid if bid > 0 else None
    except Exception:
        pass
    return None


def _current_stock_price(ticker: str) -> float | None:
    try:
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return None


# ── Position management ───────────────────────────────────────────────────────

def manage_positions(df: pd.DataFrame, today: date) -> tuple[pd.DataFrame, list[str]]:
    """
    Check all open positions for profit takes, expiries, and stop losses.
    Returns updated df and a list of event strings for logging.
    """
    events = []

    # ── Open puts ─────────────────────────────────────────────────────────────
    open_puts = df[(df['status'] == 'open') & (df['option_type'] == 'put')].copy()

    for idx, row in open_puts.iterrows():
        expiry = row['expiry'].date()
        dte    = (expiry - today).days
        n      = int(row['contracts'])
        prem   = float(row['open_premium'])
        strike = float(row['strike'])
        ticker = row['ticker']
        expiry_str = str(expiry)

        if dte <= 0:
            # Expiry day — check assignment
            cur_px = _current_stock_price(ticker)
            if cur_px is not None and cur_px < strike:
                # Assigned: now hold shares at strike cost basis
                pnl = prem * 100 * n - COMMISSION * n * 2
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = 0.0
                df.at[idx, 'close_type']    = 'assigned'
                df.at[idx, 'pnl']           = round(pnl, 2)
                df.at[idx, 'status']        = 'closed'
                # Add a share position row
                share_row = {
                    'open_date':       today, 'ticker': ticker,
                    'option_type':     'shares', 'strike': strike,
                    'expiry':          pd.NaT, 'contracts': n,
                    'open_premium':    strike,
                    'contract_symbol': f'{ticker}_shares',
                    'close_date':      pd.NaT, 'close_premium': None,
                    'close_type':      None, 'pnl': None,
                    'status':          'open',
                }
                df = pd.concat([df, pd.DataFrame([share_row])], ignore_index=True)
                events.append(f"ASSIGNED  {ticker} {n}x${strike:.0f} put — now holding {n*100} shares @ ${strike:.0f}")
            else:
                # Expired OTM — keep full premium
                pnl = prem * 100 * n - COMMISSION * n * 2
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = 0.0
                df.at[idx, 'close_type']    = 'expired_otm'
                df.at[idx, 'pnl']           = round(pnl, 2)
                df.at[idx, 'status']        = 'closed'
                events.append(f"EXPIRED   {ticker} {n}x${strike:.0f} put — full premium ${pnl:,.0f} kept")

        else:
            # Not expired — check 50% profit take
            cur_bid = _current_option_bid(ticker, expiry_str, strike, 'put')
            if cur_bid is not None and cur_bid <= prem * PROFIT_TAKE_PCT:
                pnl = (prem - cur_bid) * 100 * n - COMMISSION * n * 2
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = round(cur_bid, 2)
                df.at[idx, 'close_type']    = 'profit_take'
                df.at[idx, 'pnl']           = round(pnl, 2)
                df.at[idx, 'status']        = 'closed'
                events.append(f"50% TAKE  {ticker} {n}x${strike:.0f} put — closed @ ${cur_bid:.2f} for +${pnl:,.0f}")

    # ── Open share positions (waiting for recovery or stop) ───────────────────
    open_shares = df[(df['status'] == 'open') & (df['option_type'] == 'shares')].copy()

    for idx, row in open_shares.iterrows():
        ticker     = row['ticker']
        cost_basis = float(row['strike'])
        stop_level = round(cost_basis * (1 - STOP_LOSS_PCT), 2)
        n          = int(row['contracts'])
        cur_px     = _current_stock_price(ticker)
        if cur_px is None:
            continue

        if cur_px <= stop_level:
            # Stop triggered
            loss = (cur_px - cost_basis) * 100 * n - COMMISSION * n * 2
            df.at[idx, 'close_date']    = pd.Timestamp(today)
            df.at[idx, 'close_premium'] = round(cur_px, 2)
            df.at[idx, 'close_type']    = 'stop_loss'
            df.at[idx, 'pnl']           = round(loss, 2)
            df.at[idx, 'status']        = 'closed'
            events.append(f"STOP LOSS {ticker} {n*100} shares @ ${cur_px:.2f} (basis ${cost_basis:.2f}) — P&L ${loss:,.0f}")

    return df, events


# ── Open new positions ────────────────────────────────────────────────────────

def open_new_positions(df: pd.DataFrame, candidates: list, today: date,
                       portfolio_value: float) -> tuple[pd.DataFrame, list[str]]:
    """
    Enter up to MAX_POSITIONS new paper puts from screener candidates.
    Skips tickers already in open positions.
    """
    events = []

    open_tickers = set(
        df[(df['status'] == 'open') & (df['option_type'].isin(['put', 'shares']))]['ticker'].tolist()
    )
    n_open = len(df[(df['status'] == 'open') & (df['option_type'] == 'put')])
    slots_free = MAX_POSITIONS - n_open

    if slots_free <= 0:
        return df, ['All 5 slots occupied — no new positions opened']

    slot_cash = portfolio_value * POSITION_SIZE_PCT

    for r in candidates:
        if slots_free <= 0:
            break
        ticker = r['ticker']
        if ticker in open_tickers:
            continue

        strike   = float(r['put_strike'])
        bid      = float(r['put_bid'])
        expiry   = r['expiry']
        cash_req = strike * 100
        n        = max(1, int(slot_cash // cash_req))

        # Don't commit more than 1.5× slot size
        if n * cash_req > slot_cash * 1.5:
            n = 1

        row = {
            'open_date':       today,
            'ticker':          ticker,
            'option_type':     'put',
            'strike':          strike,
            'expiry':          pd.Timestamp(expiry),
            'contracts':       n,
            'open_premium':    bid,
            'contract_symbol': f"{ticker}_{expiry}_{strike:.0f}P",
            'close_date':      pd.NaT,
            'close_premium':   None,
            'close_type':      None,
            'pnl':             None,
            'status':          'open',
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        gross = bid * 100 * n
        events.append(f"OPENED    {ticker} {n}x ${strike:.0f} put exp {expiry} "
                      f"@ ${bid:.2f} bid  +${gross:,.0f} premium  "
                      f"(${n*cash_req:,.0f} capital)")
        open_tickers.add(ticker)
        slots_free -= 1

    return df, events


# ── Portfolio value ───────────────────────────────────────────────────────────

def _portfolio_value(df: pd.DataFrame, starting_capital: float) -> float:
    """
    Approximate current portfolio value:
      starting capital + all realised P&L so far
      (open positions are valued at their collateral, not marked to market)
    """
    realized = df[df['status'] == 'closed']['pnl'].sum() if not df.empty else 0
    return starting_capital + (realized if not pd.isna(realized) else 0)


# ── Summary printing ──────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame, events: list[str], portfolio_value: float, today: date):
    print(f"\n{'='*65}")
    print(f"  PAPER TRADING — {today.strftime('%A %B %d, %Y')}")
    print(f"{'='*65}")

    if events:
        print("\n  TODAY'S EVENTS:")
        for e in events:
            print(f"    {e}")

    open_puts   = df[(df['status'] == 'open') & (df['option_type'] == 'put')]
    open_shares = df[(df['status'] == 'open') & (df['option_type'] == 'shares')]
    closed      = df[df['status'] == 'closed']
    realized    = closed['pnl'].sum() if not closed.empty else 0

    month_start = pd.Timestamp(today.replace(day=1))
    mtd = df[(df['status'] == 'closed') &
             (pd.to_datetime(df['close_date']) >= month_start)]['pnl'].sum()
    if pd.isna(mtd): mtd = 0

    print(f"\n  PORTFOLIO SUMMARY:")
    print(f"    Estimated value : ${portfolio_value:,.0f}")
    print(f"    MTD realized P&L: ${mtd:+,.0f}")
    print(f"    All-time P&L    : ${realized:+,.0f}")
    print(f"    Open puts       : {len(open_puts)}")
    print(f"    Held shares     : {len(open_shares)}")

    if not open_puts.empty:
        print(f"\n  OPEN PUTS:")
        for _, row in open_puts.iterrows():
            exp = str(row['expiry'])[:10]
            dte = (date.fromisoformat(exp) - today).days
            print(f"    {row['ticker']:6} {int(row['contracts'])}x ${float(row['strike']):.0f} put "
                  f"exp {exp} ({dte}d)  opened @ ${float(row['open_premium']):.2f}")

    if not open_shares.empty:
        print(f"\n  HELD SHARES:")
        for _, row in open_shares.iterrows():
            cur = _current_stock_price(row['ticker'])
            basis = float(row['strike'])
            pct   = (cur / basis - 1) * 100 if cur else 0
            stop  = round(basis * (1 - STOP_LOSS_PCT), 2)
            print(f"    {row['ticker']:6} {int(row['contracts'])*100} shares  "
                  f"basis ${basis:.2f}  current ${cur:.2f} ({pct:+.1f}%)  stop ${stop:.2f}")

    if not closed.empty:
        print(f"\n  CLOSED TRADES ({len(closed)} total):")
        win = (closed['pnl'] > 0).sum()
        print(f"    Win rate: {win}/{len(closed)} ({win/len(closed)*100:.0f}%)")

    print(f"\n{'='*65}\n")


# ── Live position pricing table ───────────────────────────────────────────────

def build_live_positions_html(open_puts: pd.DataFrame, today: date) -> str:
    """Return an HTML table of open puts with live stock price, current bid,
    % decayed, and distance to the 50% profit-take trigger."""
    if open_puts.empty:
        return '<p><em>No open put positions.</em></p>'

    rows = ''
    for _, row in open_puts.iterrows():
        ticker     = row['ticker']
        strike     = float(row['strike'])
        open_prem  = float(row['open_premium'])
        expiry_str = str(row['expiry'])[:10]
        n          = int(row['contracts'])
        dte        = (date.fromisoformat(expiry_str) - today).days
        take_target = round(open_prem * PROFIT_TAKE_PCT, 2)

        cur_stock = _current_stock_price(ticker)
        cur_bid   = _current_option_bid(ticker, expiry_str, strike, 'put')

        stock_str = f'${cur_stock:.2f}' if cur_stock else 'N/A'
        otm_str   = (f'{(cur_stock - strike) / cur_stock * 100:+.1f}%'
                     if cur_stock else '—')

        if cur_bid is not None:
            decayed_pct = (open_prem - cur_bid) / open_prem * 100
            if decayed_pct >= 50:
                decay_color = '#27ae60'
                decay_label = f'{decayed_pct:.0f}% ✓ TAKE'
            elif decayed_pct >= 30:
                decay_color = '#e67e22'
                decay_label = f'{decayed_pct:.0f}%'
            else:
                decay_color = '#555'
                decay_label = f'{decayed_pct:.0f}%'
            distance = cur_bid - take_target
            dist_str  = ('→ TAKE NOW' if distance <= 0
                         else f'${distance:.2f} away')
            bid_str   = f'${cur_bid:.2f}'
        else:
            decay_color = '#999'
            decay_label = 'N/A'
            bid_str     = 'N/A'
            dist_str    = '—'

        rows += (
            f'<tr>'
            f'<td><b>{ticker}</b></td>'
            f'<td>{n}x ${strike:.0f}</td>'
            f'<td>{stock_str} ({otm_str})</td>'
            f'<td>${open_prem:.2f}</td>'
            f'<td>{bid_str}</td>'
            f'<td style="color:{decay_color};font-weight:bold">{decay_label}</td>'
            f'<td>${take_target:.2f}</td>'
            f'<td>{dist_str}</td>'
            f'<td>{dte}d</td>'
            f'</tr>\n'
        )

    return (
        '<table border="1" cellpadding="5" '
        'style="border-collapse:collapse;width:100%;font-size:13px">'
        '<tr style="background:#2c3e50;color:white">'
        '<th>Ticker</th><th>Position</th><th>Stock (vs strike)</th>'
        '<th>Opened @</th><th>Current Bid</th><th>Decayed</th>'
        '<th>50% Take @</th><th>Distance</th><th>DTE</th>'
        '</tr>'
        f'{rows}'
        '</table>'
    )


# ── Email summary ─────────────────────────────────────────────────────────────

def _send_email_summary(df: pd.DataFrame, events: list[str],
                        portfolio_value: float, today: date,
                        candidates: list | None = None):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return

    open_puts   = df[(df['status'] == 'open') & (df['option_type'] == 'put')]
    open_shares = df[(df['status'] == 'open') & (df['option_type'] == 'shares')]
    closed      = df[df['status'] == 'closed']
    realized    = closed['pnl'].sum() if not closed.empty else 0
    month_start = pd.Timestamp(today.replace(day=1))
    mtd = df[(df['status'] == 'closed') &
             (pd.to_datetime(df['close_date']) >= month_start)]['pnl'].sum()
    if pd.isna(mtd): mtd = 0
    win = (closed['pnl'] > 0).sum() if not closed.empty else 0
    total = len(closed)

    events_html = ''.join(f'<li>{e}</li>' for e in events) if events else '<li>No events today</li>'

    subject = (f"Paper Trading — {today.strftime('%a %b %d')} | "
               f"MTD ${mtd:+,.0f} | All-time ${realized:+,.0f}")

    live_table = build_live_positions_html(open_puts, today)

    cand_rows = ''
    if candidates:
        for r in candidates:
            cand_rows += (
                f'<tr><td><b>{r["ticker"]}</b></td>'
                f'<td>${r["price"]:.2f}</td>'
                f'<td>{r["iv_rank"]:.0f}</td>'
                f'<td>${r["put_strike"]} @ ${r["put_bid"]}</td>'
                f'<td>Δ{r["put_delta"]}</td>'
                f'<td>{r["put_ann_yield"]}%/yr</td>'
                f'<td>{r["dte"]}d</td></tr>\n'
            )
    cand_html = (
        '<table border="1" cellpadding="5" style="border-collapse:collapse;font-size:13px">'
        '<tr style="background:#2c3e50;color:white">'
        '<th>Ticker</th><th>Price</th><th>IVR</th><th>Strike @ Bid</th>'
        '<th>Delta</th><th>Ann Yield</th><th>DTE</th></tr>'
        f'{cand_rows}</table>'
    ) if cand_rows else '<p><em>No candidates above threshold today.</em></p>'

    html = f"""
    <html><body style="font-family:-apple-system,Arial,sans-serif;font-size:14px;max-width:780px;margin:auto;padding:20px">
    <h2 style="border-bottom:2px solid #2c3e50">Paper Trading — {today.strftime('%A %B %d, %Y')}</h2>

    <h3>Today's Events</h3>
    <ul>{events_html}</ul>

    <h3>Portfolio</h3>
    <table><tr><td>Estimated value</td><td><b>${portfolio_value:,.0f}</b></td></tr>
    <tr><td>MTD realized P&L</td><td><b>${mtd:+,.0f}</b></td></tr>
    <tr><td>All-time P&L</td><td><b>${realized:+,.0f}</b></td></tr>
    <tr><td>Win rate</td><td><b>{win}/{total}</b></td></tr></table>

    <h3>Open Positions — Live Pricing</h3>
    {live_table}

    <h3>Today's Screener Candidates</h3>
    {cand_html}
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

    print(f"  Summary emailed to {EMAIL_RECIPIENT}")


# ── Main daily run ────────────────────────────────────────────────────────────

def daily_run():
    today = date.today()

    # Skip weekends
    if today.weekday() >= 5:
        print(f"Weekend ({today}) — skipping.")
        return

    print(f"Paper trading run: {today}")

    df  = _load()
    log = _load_log()

    # Step 1: manage existing positions
    df, mgmt_events = manage_positions(df, today)

    # Step 2: run screener, open new positions
    print("  Running screener...", end='', flush=True)
    try:
        candidates = screen()
        print(f" {len(candidates)} candidates")
    except Exception as e:
        print(f" ERROR: {e}")
        candidates = []

    portfolio_value = _portfolio_value(df, STARTING_CAPITAL)
    df, open_events = open_new_positions(df, candidates, today, portfolio_value)

    all_events = mgmt_events + open_events

    # Step 3: save
    _save(df)

    # Step 4: append daily log snapshot
    realized = df[df['status'] == 'closed']['pnl'].sum() if not df.empty else 0
    month_start = pd.Timestamp(today.replace(day=1))
    mtd = df[(df['status'] == 'closed') &
             (pd.to_datetime(df['close_date']) >= month_start)]['pnl'].sum()
    log_row = {
        'date':            today,
        'portfolio_value': round(portfolio_value, 2),
        'open_positions':  int(len(df[(df['status'] == 'open') & (df['option_type'] == 'put')])),
        'mtd_realized':    round(float(mtd) if not pd.isna(mtd) else 0, 2),
        'total_realized':  round(float(realized) if not pd.isna(realized) else 0, 2),
    }
    log = pd.concat([log, pd.DataFrame([log_row])], ignore_index=True)
    _save_log(log)

    # Step 5: print summary and email
    portfolio_value = _portfolio_value(df, STARTING_CAPITAL)
    print_summary(df, all_events, portfolio_value, today)
    _send_email_summary(df, all_events, portfolio_value, today, candidates)


if __name__ == '__main__':
    daily_run()
