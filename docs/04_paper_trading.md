# Paper Trading

## Purpose

Paper trading validates that the live screener reproduces backtest trade quality before real capital is deployed. Key things being tested:

- yfinance bid quality vs actual Schwab fills
- IVR accuracy from rolling `iv_history.csv` vs ORATS baseline
- Trade frequency — enough IVR ≥ 40 candidates to keep slots full
- Position management timing — profit takes and stops firing correctly

Target: **90 days, 20+ closed trades** before going live.

---

## Engine — daily flow (`paper_trading.py`)

1. Load `data/paper_trades.csv`
2. **Manage open puts** — expiry (OTM keep premium / ITM assign shares), 50% profit take
3. **Manage shares** — stop loss at strike × 0.80
4. Run screener for fresh candidates
5. Fill free slots (skip tickers already held)
6. Save trades, append to `paper_log.csv`
7. Email end-of-day summary

### Position sizing

```python
slot_cash = portfolio_value × 0.20
n = max(1, int(slot_cash // (strike × 100)))
if n × strike × 100 > slot_cash × 1.5:
    n = 1
```

Commission: $0.65/contract/leg.

---

## Current state (as of May 1, 2026)

**Capital:** $200,000 | **Started:** April 26, 2026

### Open positions

| Ticker | Strike | Contracts | Premium | Expiry | DTE |
|--------|--------|-----------|---------|--------|-----|
| AMAT | $380 | 1 | $14.65 | May 29 | 28 |
| GDX | $90 | 4 | $2.93 | May 29 | 28 |
| CRM | $165 | 2 | $5.35 | May 29 | 28 |
| MSFT | $400 | 1 | $8.00 | May 29 | 28 |
| INTC | $90 | 4 | $4.30 | May 29 | 28 |

### Closed trades

| Ticker | Opened | Closed | P&L | Reason |
|--------|--------|--------|-----|--------|
| USO | Apr 26 | Apr 29 | +$956 | 50% take (3 days) |
| MU | Apr 26 | May 1 | +$1,469 | 50% take (5 days) |

### Performance

| Metric | Value |
|--------|-------|
| Portfolio value | $202,425 |
| MTD P&L | +$1,469 |
| All-time P&L | +$2,425 |
| Win rate | 2/2 |

---

## Go-live criteria

| Criteria | Target |
|----------|--------|
| Paper trading duration | ≥ 60 trading days |
| Closed trades | ≥ 20 |
| Win rate | ≥ 65% |
| Monthly return | 0.5–2.5% |
| Data quality issues | None |

Live execution will use the **Schwab Individual Trader API** — the screener output maps directly to an options order ticket (`ticker`, `put_strike`, `expiry`, `contracts`).
