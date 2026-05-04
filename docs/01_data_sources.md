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

## Data flow

```
ORATS FTP → ingest_orats.py → options.duckdb (Mac only)
                                ├── backtest_orats.py
                                └── iv_history bootstrap → iv_history.csv

yfinance (daily) → iv_history.py → iv_history.csv (appended daily)
                 → screener.py    (live chains)
                 → paper_trading.py (profit-take checks)
```
