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
    TICKER_SECTORS,
)
from screener import screen, _find_target_call

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

    # ── Open share positions (profitable exit check + stop loss) ─────────────
    open_shares   = df[(df['status'] == 'open') & (df['option_type'] == 'shares')].copy()
    assigned_puts = df[df['close_type'] == 'assigned']

    for idx, row in open_shares.iterrows():
        ticker     = row['ticker']
        cost_basis = float(row['strike'])
        stop_level = round(cost_basis * (1 - STOP_LOSS_PCT), 2)
        n          = int(row['contracts'])
        cur_px     = _current_stock_price(ticker)
        if cur_px is None:
            continue

        # ── Profitable exit: total cycle P&L > 0 ──────────────────────────
        put_match  = assigned_puts[assigned_puts['ticker'] == ticker]
        put_pnl    = float(put_match['pnl'].iloc[0]) if not put_match.empty else 0.0

        closed_cc  = df[(df['ticker'] == ticker) & (df['option_type'] == 'call') &
                        (df['status'] == 'closed')]
        cc_closed_pnl = float(closed_cc['pnl'].sum()) if not closed_cc.empty else 0.0

        open_cc = df[(df['ticker'] == ticker) & (df['option_type'] == 'call') &
                     (df['status'] == 'open')]
        cc_buyback = 0.0
        cc_idx     = None
        if not open_cc.empty:
            cc_row     = open_cc.iloc[0]
            cc_idx     = open_cc.index[0]
            cc_exp_str = str(cc_row['expiry'].date())
            cc_bid     = _current_option_bid(ticker, cc_exp_str, float(cc_row['strike']), 'call')
            if cc_bid is not None:
                cc_buyback = cc_bid * 100 * n + COMMISSION * n * 2

        share_pnl   = (cur_px - cost_basis) * 100 * n - COMMISSION * n * 2
        cycle_pnl   = put_pnl + cc_closed_pnl - cc_buyback + share_pnl

        if cycle_pnl > 0:
            # Close open CC first (buy back)
            if cc_idx is not None:
                cc_buyback_pnl = -(cc_buyback)
                df.at[cc_idx, 'close_date']    = pd.Timestamp(today)
                df.at[cc_idx, 'close_premium'] = round(cc_bid, 2) if cc_bid else 0.0
                df.at[cc_idx, 'close_type']    = 'cycle_exit'
                df.at[cc_idx, 'pnl']           = round(
                    (float(open_cc.iloc[0]['open_premium']) - (cc_bid or 0)) * 100 * n - COMMISSION * n * 2, 2)
                df.at[cc_idx, 'status']        = 'closed'
            # Close shares
            df.at[idx, 'close_date']    = pd.Timestamp(today)
            df.at[idx, 'close_premium'] = round(cur_px, 2)
            df.at[idx, 'close_type']    = 'cycle_exit'
            df.at[idx, 'pnl']           = round(share_pnl, 2)
            df.at[idx, 'status']        = 'closed'
            events.append(f"CYCLE EXIT {ticker} — sold {n*100} shares @ ${cur_px:.2f}  "
                          f"full cycle P&L ${cycle_pnl:+,.0f}  "
                          f"(put ${put_pnl:+,.0f} + CC ${cc_closed_pnl:+,.0f} + shares ${share_pnl:+,.0f})")
            continue

        # ── Stop loss ─────────────────────────────────────────────────────
        if cur_px <= stop_level:
            loss = (cur_px - cost_basis) * 100 * n - COMMISSION * n * 2
            df.at[idx, 'close_date']    = pd.Timestamp(today)
            df.at[idx, 'close_premium'] = round(cur_px, 2)
            df.at[idx, 'close_type']    = 'stop_loss'
            df.at[idx, 'pnl']           = round(loss, 2)
            df.at[idx, 'status']        = 'closed'
            events.append(f"STOP LOSS {ticker} {n*100} shares @ ${cur_px:.2f} (basis ${cost_basis:.2f}) — P&L ${loss:,.0f}")

    # ── Open covered calls (50% take or expiry/assignment) ───────────────────
    open_calls = df[(df['status'] == 'open') & (df['option_type'] == 'call')].copy()

    for idx, row in open_calls.iterrows():
        ticker     = row['ticker']
        expiry     = row['expiry'].date()
        dte        = (expiry - today).days
        n          = int(row['contracts'])
        prem       = float(row['open_premium'])
        strike     = float(row['strike'])
        expiry_str = str(expiry)

        if dte <= 0:
            cur_px = _current_stock_price(ticker)
            if cur_px is not None and cur_px >= strike:
                # Called away — close shares and call together
                put_match = assigned_puts[assigned_puts['ticker'] == ticker]
                put_pnl   = float(put_match['pnl'].iloc[0]) if not put_match.empty else 0.0
                share_idx = df[(df['status'] == 'open') & (df['option_type'] == 'shares') & (df['ticker'] == ticker)].index
                share_basis = float(df.at[share_idx[0], 'strike']) if len(share_idx) else strike
                share_pnl = (strike - share_basis) * 100 * n - COMMISSION * n * 2
                call_pnl  = prem * 100 * n - COMMISSION * n * 2
                # Close the call
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = 0.0
                df.at[idx, 'close_type']    = 'cc_assigned'
                df.at[idx, 'pnl']           = round(call_pnl, 2)
                df.at[idx, 'status']        = 'closed'
                # Close the shares
                if len(share_idx):
                    df.at[share_idx[0], 'close_date']    = pd.Timestamp(today)
                    df.at[share_idx[0], 'close_premium'] = round(cur_px, 2)
                    df.at[share_idx[0], 'close_type']    = 'cc_assigned'
                    df.at[share_idx[0], 'pnl']           = round(share_pnl, 2)
                    df.at[share_idx[0], 'status']        = 'closed'
                total_cycle = put_pnl + call_pnl + share_pnl
                events.append(f"CC ASSIGNED {ticker} — shares called away @ ${strike:.0f}  "
                              f"full cycle P&L ${total_cycle:+,.0f}")
            else:
                # Call expired OTM — keep premium, keep shares
                call_pnl = prem * 100 * n - COMMISSION * n * 2
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = 0.0
                df.at[idx, 'close_type']    = 'expired_otm'
                df.at[idx, 'pnl']           = round(call_pnl, 2)
                df.at[idx, 'status']        = 'closed'
                events.append(f"CC EXPIRED {ticker} ${strike:.0f} call — premium ${call_pnl:+,.0f} kept, "
                              f"shares retained")
        else:
            # 50% profit take on the call
            cur_bid = _current_option_bid(ticker, expiry_str, strike, 'call')
            if cur_bid is not None and cur_bid <= prem * PROFIT_TAKE_PCT:
                call_pnl = (prem - cur_bid) * 100 * n - COMMISSION * n * 2
                df.at[idx, 'close_date']    = pd.Timestamp(today)
                df.at[idx, 'close_premium'] = round(cur_bid, 2)
                df.at[idx, 'close_type']    = 'profit_take'
                df.at[idx, 'pnl']           = round(call_pnl, 2)
                df.at[idx, 'status']        = 'closed'
                events.append(f"CC 50% TAKE {ticker} ${strike:.0f} call — closed @ ${cur_bid:.2f} "
                              f"for +${call_pnl:+,.0f}")

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
    # Shares from assignment still occupy a slot until sold
    n_open = len(df[(df['status'] == 'open') & (df['option_type'].isin(['put', 'shares']))])
    slots_free = MAX_POSITIONS - n_open

    if slots_free <= 0:
        return df, ['All 5 slots occupied — no new positions opened']

    # Deduct capital already tied up in held shares from available cash
    shares_df = df[(df['status'] == 'open') & (df['option_type'] == 'shares')]
    shares_capital = sum(
        float(r['strike']) * int(r['contracts']) * 100
        for _, r in shares_df.iterrows()
    )
    available_cash = portfolio_value - shares_capital
    slot_cash = available_cash * POSITION_SIZE_PCT

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


# ── Write covered calls on held shares ────────────────────────────────────────

def write_covered_calls(df: pd.DataFrame, today: date) -> tuple[pd.DataFrame, list[str]]:
    """
    For each open share position with no existing covered call, find and write
    a covered call at strike >= net_basis (original strike minus put premium collected).
    """
    events = []
    open_shares = df[(df['status'] == 'open') & (df['option_type'] == 'shares')]
    if open_shares.empty:
        return df, events

    assigned_puts = df[df['close_type'] == 'assigned']
    open_calls    = df[(df['status'] == 'open') & (df['option_type'] == 'call')]
    covered_tickers = set(open_calls['ticker'].tolist())

    for _, row in open_shares.iterrows():
        ticker   = row['ticker']
        if ticker in covered_tickers:
            continue  # already have a call open

        n        = int(row['contracts'])
        basis    = float(row['strike'])

        # Net basis: original strike minus per-share premium banked on the assigned put
        put_match = assigned_puts[assigned_puts['ticker'] == ticker]
        put_pnl   = float(put_match['pnl'].iloc[0]) if not put_match.empty else 0.0
        net_basis = basis - (put_pnl / (n * 100))

        try:
            S = float(yf.Ticker(ticker).fast_info.last_price)
        except Exception:
            continue

        call = _find_target_call(ticker, S, net_basis, today)
        if call is None:
            continue

        call_row = {
            'open_date':       today,
            'ticker':          ticker,
            'option_type':     'call',
            'strike':          call['strike'],
            'expiry':          pd.Timestamp(call['expiry']),
            'contracts':       n,
            'open_premium':    call['bid'],
            'contract_symbol': f"{ticker}_{call['expiry']}_{call['strike']:.0f}C",
            'close_date':      pd.NaT,
            'close_premium':   None,
            'close_type':      None,
            'pnl':             None,
            'status':          'open',
        }
        df = pd.concat([df, pd.DataFrame([call_row])], ignore_index=True)
        gross = call['bid'] * 100 * n
        events.append(f"CC OPENED {ticker} {n}x ${call['strike']:.0f} call exp "
                      f"{call['expiry_str']} @ ${call['bid']:.2f} bid  "
                      f"+${gross:,.0f} premium  (net basis ${net_basis:.2f})")
        covered_tickers.add(ticker)

    return df, events


# ── Portfolio value ───────────────────────────────────────────────────────────

def _portfolio_value(df: pd.DataFrame, starting_capital: float) -> float:
    """
    Approximate current portfolio value:
      starting capital + realised P&L + mark-to-market on held shares
    """
    realized = df[df['status'] == 'closed']['pnl'].sum() if not df.empty else 0
    realized = realized if not pd.isna(realized) else 0

    # Mark held shares at current price
    shares_df = df[(df['status'] == 'open') & (df['option_type'] == 'shares')]
    shares_mtm = 0.0
    for _, row in shares_df.iterrows():
        cur_px = _current_stock_price(row['ticker'])
        if cur_px:
            cost_basis = float(row['strike']) * int(row['contracts']) * 100
            shares_mtm += (cur_px - float(row['strike'])) * int(row['contracts']) * 100
            # capital is already accounted for in starting_capital — only add the unrealized gain/loss

    return starting_capital + realized + shares_mtm


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
        completed  = closed[closed['close_type'] != 'assigned']
        assigned_n = len(closed[closed['close_type'] == 'assigned'])
        win = (completed['pnl'] > 0).sum() if not completed.empty else 0
        total_c = len(completed)
        pct = win / total_c * 100 if total_c else 0
        assigned_note = f"  ({assigned_n} assigned/pending)" if assigned_n else ""
        print(f"\n  CLOSED TRADES ({len(closed)} total):")
        print(f"    Win rate: {win}/{total_c} ({pct:.0f}%){assigned_note}")

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

        sector = TICKER_SECTORS.get(ticker, 'Other')
        rows += (
            f'<tr>'
            f'<td><b>{ticker}</b></td>'
            f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
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
        '<th>Ticker</th><th>Sector</th><th>Position</th><th>Stock (vs strike)</th>'
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
    completed  = closed[closed['close_type'] != 'assigned'] if not closed.empty else closed
    assigned_n = len(closed[closed['close_type'] == 'assigned']) if not closed.empty else 0
    win   = (completed['pnl'] > 0).sum() if not completed.empty else 0
    total = len(completed)

    events_html = ''.join(f'<li>{e}</li>' for e in events) if events else '<li>No events today</li>'

    subject = (f"Paper Trading — {today.strftime('%a %b %d')} | "
               f"MTD ${mtd:+,.0f} | All-time ${realized:+,.0f}")

    live_table = build_live_positions_html(open_puts, today)

    cand_rows = ''
    if candidates:
        seen_sectors: set = set()
        open_tickers = set(open_puts['ticker'].tolist()) if not open_puts.empty else set()
        for r in candidates:
            sector = r.get('sector', 'Other')
            if sector in seen_sectors:
                continue
            seen_sectors.add(sector)
            held = '✓ open' if r['ticker'] in open_tickers else '—'
            held_style = 'color:#e67e22;font-weight:bold' if held != '—' else 'color:#7f8c8d'
            cand_rows += (
                f'<tr>'
                f'<td style="color:#7f8c8d;font-size:12px">{sector}</td>'
                f'<td><b>{r["ticker"]}</b></td>'
                f'<td style="{held_style}">{held}</td>'
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
        '<th>Sector</th><th>Ticker</th><th>Held?</th><th>Price</th><th>IVR</th>'
        '<th>Strike @ Bid</th><th>Delta</th><th>Ann Yield</th><th>DTE</th></tr>'
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
    <tr><td>Win rate</td><td><b>{win}/{total}</b>{f" · {assigned_n} assigned/pending" if assigned_n else ""}</td></tr></table>

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

    # Step 2b: write covered calls on any uncovered share positions
    df, cc_events = write_covered_calls(df, today)

    all_events = mgmt_events + open_events + cc_events

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
