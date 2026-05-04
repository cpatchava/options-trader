# Backtest Results

**Data:** ORATS SMV Strikes 2020–2026, real bid fills, $0.65/contract commission.  
**Engine:** `backtest_orats.py` — identical rules to live screener.  
**Starting capital:** $100,000.

---

## Walk-forward validation

Strategy tuned on 2020–2022 only, then run blind on 2023–2026.

| Period | Annual return | Sharpe |
|--------|--------------|--------|
| In-sample (2020–2022) | 23.9% | 1.25 |
| Out-of-sample (2023–2026) | 38.9% | 2.54 |
| **Retention ratio** | **163%** | — |

[Retention ratio](https://en.wikipedia.org/wiki/Walk_forward_optimization) > 100% means out-of-sample beat in-sample — strong evidence against overfitting.

---

## Starting-date sensitivity

| Start | Annual return | Sharpe | Worst month |
|-------|--------------|--------|-------------|
| 2020 | 26.2% | 1.58 | -9.7% |
| 2021 | 24.4% | 1.44 | -9.7% |
| 2022 | 19.4% | 1.30 | -9.7% |
| 2023 | 28.5% | 2.20 | -4.9% |

All entry points positive — strategy is not sensitive to timing of entry.

---

## After-tax returns

Options premium is taxed as [ordinary income](https://en.wikipedia.org/wiki/Ordinary_income) (short-term gains). Modelled at 37% federal rate, paid annually from portfolio proceeds.

| Metric | Value |
|--------|-------|
| Pre-tax annual return | 26.2% |
| Pre-tax final value | $434,187 |
| After-tax annual return | 19.5% |
| After-tax final value | $307,794 |
| Total tax paid | $128,643 |
| SPY after 15% [LTCG](https://en.wikipedia.org/wiki/Capital_gains_tax_in_the_United_States) | 13.1%/yr |
| **Outperformance vs SPY** | **+6.4 pp/yr** |

---

## Stop-loss clustering

| Metric | Value |
|--------|-------|
| Total stops (2020–2026) | 89 |
| Isolated | 6 (7%) |
| Clustered | 83 (93%) |
| Total stop P&L | -$427,678 |

Stops cluster during market stress — expected for a correlated equity portfolio. Worst windows:

| Window | Stops | P&L |
|--------|-------|-----|
| Feb 20, 2026 | 5 | -$53,698 |
| Feb 4, 2026 | 5 | -$53,221 |
| Mar 2, 2026 | 4 | -$49,913 |

The strategy recovers from these drawdowns — stop losses contain damage, they don't eliminate it.

---

## Summary

| Metric | Value |
|--------|-------|
| Period | Jan 2020 – Apr 2026 |
| After-tax annual return | 19.5% |
| Sharpe ratio | 1.58 |
| Walk-forward retention | 163% |
| Max monthly drawdown | -9.7% |
| Final after-tax value (from $100K) | $307,794 |
