import os
from pathlib import Path
from dotenv import load_dotenv

# Load project-level .env first, then fall back to ~/.env
load_dotenv(Path(__file__).parent / '.env')
load_dotenv(Path.home() / '.env')

# ── Universe ───────────────────────────────────────────────────────────────────
# ~80 liquid S&P 500 names across all sectors — broad enough that there are
# always several IVR≥40 candidates each day.  Price filter ($20-$500) in the
# screener automatically excludes anything above the single-contract limit.
WATCHLIST = [
    # Technology
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'AMD', 'INTC',
    'CRM', 'ORCL', 'IBM', 'QCOM', 'TXN', 'MU', 'AMAT', 'ADSK',
    'PANW', 'CRWD', 'CSCO', 'AVGO', 'SNOW', 'PLTR',
    # Financials
    'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'BLK', 'AXP', 'COF',
    'USB', 'SCHW', 'PNC',
    # Healthcare
    'JNJ', 'UNH', 'PFE', 'ABBV', 'MRK', 'LLY', 'BMY', 'AMGN',
    'GILD', 'CVS', 'CI', 'VRTX',
    # Energy
    'XOM', 'CVX', 'COP', 'OXY', 'EOG', 'SLB', 'MPC', 'VLO', 'DVN',
    # Consumer staples & discretionary
    'KO', 'PEP', 'WMT', 'TGT', 'COST', 'MCD', 'NKE', 'HD', 'LOW',
    'TJX', 'DG', 'SBUX', 'DIS',
    # Industrial
    'CAT', 'DE', 'GE', 'HON', 'RTX', 'LMT', 'BA', 'ETN',
    # Materials & mining
    'FCX', 'NEM', 'CLF',
    # Communication & media
    'NFLX', 'CMCSA', 'VZ', 'T',
    # Commodity ETFs with liquid options
    'GLD', 'SLV',
]

STARTING_CAPITAL      = 200_000
TARGET_MONTHLY_RETURN = 0.01   # 1% per month

# ── Options strategy parameters ────────────────────────────────────────────────
DELTA_LOW  = 0.25   # sell puts with delta between these values (absolute)
DELTA_HIGH = 0.35   # matches backtest exactly
DTE_MIN    = 21
DTE_MAX    = 45
PROFIT_TAKE_PCT = 0.50   # close at 50% of premium captured

CC_DELTA_LOW  = 0.25     # covered call delta range
CC_DELTA_HIGH = 0.35

STOP_LOSS_PCT = 0.20     # cut share position if stock falls >20% below put strike

# ── IVR filter ─────────────────────────────────────────────────────────────────
MIN_IVR = 40   # only sell premium when IV rank ≥ 40

# ── Capital allocation ─────────────────────────────────────────────────────────
MAX_POSITIONS     = 5
POSITION_SIZE_PCT = 0.20   # each position ≤ 20% of portfolio value
CASH_BUFFER_PCT   = 0.10   # keep ≥ 10% of portfolio as free cash

# ── Market assumptions ─────────────────────────────────────────────────────────
RISK_FREE_RATE    = 0.045
HV_LOOKBACK_DAYS  = 30     # used for fallback HV calc if IV history is thin

# ── Quality filters ────────────────────────────────────────────────────────────
MIN_STOCK_PRICE      = 20.0
MAX_STOCK_PRICE      = 500.0
MIN_OPTIONS_OI       = 500
MIN_OPTIONS_VOLUME   = 50
MAX_SPREAD_PCT       = 0.20
EARNINGS_BUFFER_DAYS = 7

# ── Legacy — kept for screener BSM fallback ────────────────────────────────────
TARGET_DELTA      = DELTA_LOW        # single-value alias used by old BSM path
IV_PREMIUM_FACTOR = 1.10             # HV → IV estimate when history is thin
DTE_TARGET        = 35

# ── Commission ─────────────────────────────────────────────────────────────────
COMMISSION_PER_CONTRACT = 0.65

# ── Email ──────────────────────────────────────────────────────────────────────
EMAIL_RECIPIENT    = 'cpatchava@gmail.com'
GMAIL_ADDRESS      = os.getenv('GMAIL_ADDRESS')
GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD')
REPORT_SEND_HOUR   = 8   # 8 AM local time
