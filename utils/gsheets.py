"""
Google Sheets integration via public CSV export.

The sheet must be set to "Anyone with the link → Viewer".
No credentials or service account needed — pandas reads the CSV URL directly.

Sheet structure:
  "Traders" tab  — config: name, email, tab, capital, monthly_target_pct
  One tab per trader — their actual trades (see TRADE_COLUMNS below)
"""

import pandas as pd
from config import GOOGLE_SHEET_ID

TRADE_COLUMNS = [
    'open_date', 'ticker', 'type', 'strike', 'expiry',
    'contracts', 'premium', 'status', 'close_date', 'close_price', 'notes',
]


def _csv_url(tab: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={tab.replace(' ', '%20')}"
    )


def get_traders() -> list[dict]:
    """Read the Traders config tab. Returns list of trader dicts."""
    df = pd.read_csv(_csv_url('Traders'))
    df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]
    # tolerate common typo
    if 'montly_target_pct' in df.columns and 'monthly_target_pct' not in df.columns:
        df = df.rename(columns={'montly_target_pct': 'monthly_target_pct'})
    df = df[df['email'].notna() & (df['email'].astype(str).str.strip() != '')]
    return df.to_dict('records')


def get_trades(tab_name: str) -> pd.DataFrame:
    """Read a trader's tab. Returns DataFrame with normalised columns."""
    df = pd.read_csv(_csv_url(tab_name))

    if df.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]

    for col in ['strike', 'contracts', 'premium', 'close_price']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    for col in ['open_date', 'expiry', 'close_date']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')

    if 'status' in df.columns:
        df['status'] = df['status'].str.lower().str.strip()

    df = df.dropna(subset=['ticker']).reset_index(drop=True)
    df = df[df['ticker'].astype(str).str.strip() != '']

    return df


def compute_metrics(df: pd.DataFrame, capital: float, monthly_target_pct: float) -> dict:
    """Compute P&L metrics from a trader's trade DataFrame."""
    from datetime import date

    COMMISSION = 0.65

    empty = {
        'open_puts': pd.DataFrame(),
        'open_shares': pd.DataFrame(),
        'open_calls': pd.DataFrame(),
        'closed': pd.DataFrame(),
        'mtd': 0.0,
        'all_time': 0.0,
        'win_rate': 0.0,
        'wins': 0,
        'total_closed': 0,
        'target_monthly': capital * monthly_target_pct,
        'shares_locked': 0.0,
        'puts_locked': 0.0,
        'total_locked': 0.0,
        'capital_free': capital,
        'utilization_pct': 0.0,
    }

    if df.empty:
        return empty

    closed  = df[df['status'] == 'closed'].copy()
    open_df = df[df['status'] == 'open'].copy()

    def _pnl(row):
        n  = int(row.get('contracts', 1) or 1)
        op = float(row.get('premium', 0) or 0)
        cp = float(row.get('close_price', 0) or 0)
        return (op - cp) * 100 * n - COMMISSION * n * 2

    if not closed.empty:
        closed['pnl'] = closed.apply(_pnl, axis=1)
    else:
        closed['pnl'] = []

    today       = pd.Timestamp(date.today())
    month_start = pd.Timestamp(today.replace(day=1))
    mtd_rows    = closed[pd.to_datetime(closed['close_date']) >= month_start]
    mtd         = float(mtd_rows['pnl'].sum()) if not mtd_rows.empty else 0.0
    all_time    = float(closed['pnl'].sum()) if not closed.empty else 0.0

    # Exclude assigned trades — cycle isn't complete until shares resolve
    completed  = (closed[closed['close_type'] != 'assigned']
                  if 'close_type' in closed.columns else closed)
    wins       = int((completed['pnl'] > 0).sum()) if not completed.empty else 0
    total_cl   = len(completed)
    win_rate   = round(wins / total_cl * 100, 1) if total_cl > 0 else 0.0

    type_col   = open_df['type'].str.lower() if not open_df.empty else pd.Series(dtype=str)
    open_puts  = open_df[type_col == 'put']   if not open_df.empty else pd.DataFrame()
    open_shares= open_df[type_col == 'shares']if not open_df.empty else pd.DataFrame()
    open_calls = open_df[type_col == 'call']  if not open_df.empty else pd.DataFrame()

    # Capital allocation
    # Shares: locked = cost basis (strike) × actual share count
    shares_locked = float(
        (open_shares['strike'] * open_shares['contracts']).sum()
    ) if not open_shares.empty else 0.0

    # Puts: locked = collateral required = strike × 100 × contracts
    puts_locked = float(
        (open_puts['strike'] * open_puts['contracts'] * 100).sum()
    ) if not open_puts.empty else 0.0

    total_locked = shares_locked + puts_locked
    capital_free = capital - total_locked

    return {
        'open_puts':      open_puts,
        'open_shares':    open_shares,
        'open_calls':     open_calls,
        'closed':         closed,
        'mtd':            mtd,
        'all_time':       all_time,
        'win_rate':       win_rate,
        'wins':           wins,
        'total_closed':   total_cl,
        'target_monthly': capital * monthly_target_pct,
        'shares_locked':  shares_locked,
        'puts_locked':    puts_locked,
        'total_locked':   total_locked,
        'capital_free':   capital_free,
        'utilization_pct': round(total_locked / capital * 100, 1) if capital else 0.0,
    }
