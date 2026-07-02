# Data Sources

## Backtest — ORATS SMV Strikes

[ORATS](https://orats.com) provides end-of-day historical options chains with real bid/ask prices and model Greeks back to 2007. Data is downloaded via FTPS as daily ZIPs and stored in a local DuckDB database (`data/options.duckdb`, ~55 GB for 2020–2026) by `ingest_orats.py`.

Columns kept:

```
trade_date, ticker, stkPx, expirDate, yte, strike,
cBidPx, cAskPx, pBidPx, pAskPx, cVolu, cOi, pVolu, pOi,
smoothSmvVol, delta, gamma, theta, vega
```

Sample row:

```
trade_date  ticker  stkPx   expirDate   strike  pBidPx  pAskPx  smoothSmvVol  delta
2023-06-15  AAPL    184.92  2023-07-21  180.0   2.15    2.20    0.2341        -0.2887
```

## Live data — yfinance

| Use | Call | Returns |
|-----|------|---------|
| IV history | `t.option_chain(exp)` | `impliedVolatility` per strike |
| Stock price | `t.fast_info.last_price` | last trade |
| Option chain | `t.option_chain(exp_str)` | bid, ask, OI, volume, IV |
| Expiry list | `t.options` | ISO date strings |
| Earnings | `t.calendar` | next earnings date |

Notable limitations: delayed outside market hours, no historical chains, some ETFs return HTTP 404 on fundamentals endpoints.

## IV history store

`utils/iv_history.py` maintains `data/iv_history.csv` — daily ATM put IV per ticker, used to compute IV Rank.

```
date,       ticker, iv,     source
2026-04-28, MSFT,   0.4084, yfinance
```

Bootstrapped from ORATS on day one (137,000 readings, 87 tickers). After 252 trading days of live yfinance readings the store is fully self-contained.

### IV fetch with two-layer validation

Early-morning option quotes are often thin or zero. `update()` is called multiple times per day (noon and 3 PM via cron) and **overwrites** today's stored value each run, so the latest mid/late-day read wins.

Each fetch uses two validation layers before accepting a reading:

1. **Cross-expiry check** — fetch ATM IV for the nearest ~21 DTE and ~42 DTE expiries. If they agree within 40%, average them. If one is near-zero and the other is normal, use the higher (bad reads are almost always near-zero). This catches the case where a single expiry has thin quoting.

2. **HV floor** — compute 30-day realized volatility (HV30) from the past 45 days of daily closes. If the candidate IV is below 70% of HV30, reject it. IV should structurally exceed realized vol (volatility risk premium); a reading below that floor is almost certainly a bad early-morning data point.

If a reading is rejected, the ticker's previous value is kept unchanged for the day.

### `update_iv.py` — standalone refresh

Run via `run_update_iv.sh` by cron at noon and 3 PM ET. Fetches IV for the full watchlist and overwrites any today rows for successfully-fetched tickers. Logs to `iv_update.log`.

## Google Sheets — real trades

`utils/gsheets.py` reads real trade data from a public Google Sheet (no credentials needed — sheet must be set to "Anyone with the link → Viewer"). The sheet ID is configured in `config.py` as `GOOGLE_SHEET_ID`.

Sheet structure:
- **Traders** tab — one row per trader with: `name`, `email`, `tab`, `capital`, `monthly_target_pct`
- **Per-trader tabs** — each row is a trade: `open_date`, `ticker`, `type` (put/call/shares), `strike`, `expiry`, `contracts`, `premium`, `status`, `close_date`, `close_price`, `notes`

P&L calculation differs by type:
- Options: `(premium - close_price) × 100 × contracts - commission × contracts × 2`
- Shares: `(close_price - premium) × share_count` (no ×100 multiplier)

## Data flow

```
ORATS FTP → ingest_orats.py → options.duckdb (Mac only)
                                ├── backtest_orats.py
                                └── iv_history bootstrap → iv_history.csv

yfinance (noon + 3 PM) → update_iv.py → iv_history.csv (overwrites today)
yfinance (on demand)   → screener.py    (live chains)
                       → paper_trading.py (profit-take checks)
                       → real_report.py   (close ask prices, roll analysis)

Google Sheets (10:30 AM) → gsheets.py → real_report.py
```
