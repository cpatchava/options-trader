"""
Download ORATS SMV Strikes data via FTP, filter to essential columns,
and load into DuckDB for fast backtesting queries.

Run:    python ingest_orats.py
Resume: safe to re-run — already-ingested dates are skipped automatically.

DB location: ~/dev/options-trader/data/options.duckdb
~15-20GB on disk for 2020-2026.
"""

import io
import os
import sys
import zipfile
import time
from datetime import datetime
from ftplib import FTP_TLS
from pathlib import Path

import duckdb
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
FTP_HOST  = 'us3.hostedftp.com'
FTP_USER  = os.getenv('ORATS_FTP_USER')
FTP_PASS  = os.getenv('ORATS_FTP_PASS')
FTP_BASE  = '/smvstrikes'
YEARS     = list(range(2020, 2027))
DB_PATH   = Path(__file__).parent / 'data' / 'options.duckdb'

# Only keep what the backtest and screener actually need
KEEP_COLS = [
    'trade_date', 'ticker', 'stkPx', 'expirDate', 'yte', 'strike',
    'cBidPx', 'cAskPx', 'pBidPx', 'pAskPx',
    'cVolu', 'cOi', 'pVolu', 'pOi',
    'smoothSmvVol', 'delta', 'gamma', 'theta', 'vega',
]


# ── Database setup ─────────────────────────────────────────────────────────────

def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS options (
            trade_date   DATE,
            ticker       VARCHAR,
            stkPx        FLOAT,
            expirDate    DATE,
            yte          FLOAT,
            strike       FLOAT,
            cBidPx       FLOAT,
            cAskPx       FLOAT,
            pBidPx       FLOAT,
            pAskPx       FLOAT,
            cVolu        INTEGER,
            cOi          INTEGER,
            pVolu        INTEGER,
            pOi          INTEGER,
            smoothSmvVol FLOAT,
            delta        FLOAT,
            gamma        FLOAT,
            theta        FLOAT,
            vega         FLOAT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingested_dates (
            trade_date DATE PRIMARY KEY
        )
    """)
    # Index for fast ticker+date queries
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticker_date
        ON options (ticker, trade_date)
    """)


def already_ingested(con, date_str: str) -> bool:
    result = con.execute(
        "SELECT 1 FROM ingested_dates WHERE trade_date = ?", [date_str]
    ).fetchone()
    return result is not None


def mark_ingested(con, date_str: str):
    con.execute("INSERT OR IGNORE INTO ingested_dates VALUES (?)", [date_str])


# ── FTP helpers ────────────────────────────────────────────────────────────────

def ftp_connect() -> FTP_TLS:
    ftp = FTP_TLS(FTP_HOST, timeout=60)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.prot_p()   # switch to encrypted data channel
    return ftp


def ftp_list_files(ftp: FTP_TLS, year: int) -> list[str]:
    files = []
    ftp.retrlines(f'LIST {FTP_BASE}/{year}/', files.append)
    names = []
    for line in files:
        parts = line.split()
        if parts:
            name = parts[-1]
            if name.endswith('.zip'):
                names.append(name)
    return sorted(names)


def ftp_download_to_memory(ftp: FTP_TLS, remote_path: str) -> bytes:
    buf = io.BytesIO()
    ftp.retrbinary(f'RETR {remote_path}', buf.write)
    buf.seek(0)
    return buf.read()


# ── CSV processing ─────────────────────────────────────────────────────────────

def parse_and_filter(zip_bytes: bytes) -> pd.DataFrame | None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_name = next((n for n in zf.namelist() if n.endswith('.csv')), None)
        if not csv_name:
            return None
        with zf.open(csv_name) as f:
            df = pd.read_csv(f, low_memory=False)

    # Keep only needed columns (gracefully handle any missing)
    available = [c for c in KEEP_COLS if c in df.columns]
    df = df[available].copy()

    # Parse dates
    for date_col in ('trade_date', 'expirDate'):
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors='coerce').dt.date

    # Numeric coercion
    for col in df.columns:
        if col not in ('trade_date', 'expirDate', 'ticker'):
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # Clip Greeks to float32 range — COVID-era deep OTM options produce
    # astronomically large gamma values (e.g. 1e+113) that are numerical
    # artifacts and meaningless for strategy use.
    float32_max = 3.4e38
    for col in ('gamma', 'theta', 'vega', 'delta', 'smoothSmvVol'):
        if col in df.columns:
            df[col] = df[col].clip(-float32_max, float32_max)

    return df


# ── Main ingest loop ───────────────────────────────────────────────────────────

def run():
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    init_db(con)

    total_ingested = con.execute("SELECT COUNT(*) FROM ingested_dates").fetchone()[0]
    print(f"\nORATS → DuckDB ingest")
    print(f"DB: {DB_PATH}")
    print(f"Already ingested: {total_ingested} trading days\n")

    ftp = ftp_connect()
    print(f"Connected to {FTP_HOST}\n")

    grand_total = 0

    for year in YEARS:
        try:
            files = ftp_list_files(ftp, year)
        except Exception as e:
            print(f"  {year}: could not list — {e}")
            continue

        pending = []
        for fname in files:
            # Extract date string from filename e.g. ORATS_SMV_Strikes_20200102.zip
            date_str = fname.replace('ORATS_SMV_Strikes_', '').replace('.zip', '')
            try:
                dt = datetime.strptime(date_str, '%Y%m%d').date()
            except ValueError:
                continue
            if not already_ingested(con, str(dt)):
                pending.append((fname, dt))

        print(f"  {year}: {len(files)} files on server, {len(pending)} to ingest")

        for i, (fname, dt) in enumerate(pending, 1):
            remote_path = f'{FTP_BASE}/{year}/{fname}'
            t0 = time.time()

            try:
                raw = ftp_download_to_memory(ftp, remote_path)
                df  = parse_and_filter(raw)

                if df is None or df.empty:
                    print(f"    [{i}/{len(pending)}] {fname} — empty, skipping")
                    continue

                con.execute("INSERT INTO options SELECT * FROM df")
                mark_ingested(con, str(dt))

                elapsed = time.time() - t0
                mb = len(raw) / 1_048_576
                rows = len(df)
                print(f"    [{i}/{len(pending)}] {fname}  {rows:>7,} rows  {mb:.1f}MB  {elapsed:.1f}s")
                grand_total += 1

            except Exception as e:
                print(f"    [{i}/{len(pending)}] {fname} — ERROR: {e}")
                # Reconnect on FTP timeout
                try:
                    ftp = ftp_connect()
                except Exception:
                    pass
                continue

        # Print DB size after each year
        db_mb = DB_PATH.stat().st_size / 1_048_576
        print(f"  ✓ {year} done — DB size: {db_mb:.0f} MB\n")

    ftp.quit()

    # Final stats
    total = con.execute("SELECT COUNT(*) FROM ingested_dates").fetchone()[0]
    rows  = con.execute("SELECT COUNT(*) FROM options").fetchone()[0]
    db_gb = DB_PATH.stat().st_size / 1_073_741_824
    print(f"Ingest complete.")
    print(f"  Trading days : {total}")
    print(f"  Total rows   : {rows:,}")
    print(f"  DB size      : {db_gb:.2f} GB")

    # Build earnings calendar from IV crush detection
    print("\nBuilding earnings calendar...")
    from build_earnings_calendar import build as build_earnings
    build_earnings(con)

    con.close()


if __name__ == '__main__':
    run()
