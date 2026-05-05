# Strategy — The Wheel

The **Wheel Strategy** is a systematic options income approach: sell cash-secured puts → if assigned, sell covered calls → repeat. The institutional benchmark is the [CBOE S&P 500 PutWrite Index (PUT)](https://www.cboe.com/us/indices/dashboard/PUT/), which runs the same mechanic systematically against the S&P 500.

The economic basis is the [variance risk premium](https://en.wikipedia.org/wiki/Variance_risk_premium) — implied volatility persistently exceeds realised volatility, so option sellers collect a structural edge over time. This is well-documented in academic literature (see References).

---

## Key terms

| Term | Definition | Source |
|------|-----------|--------|
| [Implied Volatility (IV)](https://en.wikipedia.org/wiki/Implied_volatility) | Market's annualised forward vol estimate, derived from option prices | yfinance `impliedVolatility`; ORATS `smoothSmvVol` |
| IV Rank (IVR) | Percentile of today's IV vs 52-week range: `(IV - low) / (high - low) × 100` | `utils/iv_history.get_ivr()` from rolling `iv_history.csv` |
| [Delta (Δ)](https://en.wikipedia.org/wiki/Greeks_(finance)#Delta) | $/$ sensitivity of option price to stock move; also ≈ probability of expiring ITM | BSM via `utils/bsm.delta()`; ORATS `delta` in backtest |
| [Theta (Θ)](https://en.wikipedia.org/wiki/Greeks_(finance)#Theta) | Daily time decay in option value — positive for sellers | Informational only; not directly used in code |
| [DTE](https://en.wikipedia.org/wiki/Expiration_(options)) | Calendar days until expiry | Computed from expiry date |
| [Cash-secured put](https://en.wikipedia.org/wiki/Put_option#Writing_a_put) | Short put backed by full cash collateral (strike × 100) | Core position type |
| [Assignment](https://en.wikipedia.org/wiki/Option_(finance)) | Put exercised at expiry → we buy 100 shares at strike | Managed in `paper_trading.manage_positions()` |
| [Covered call](https://en.wikipedia.org/wiki/Covered_call) | Short call against held shares, strike ≥ cost basis | Second leg of the wheel |
| 50% profit take | Close when bid ≤ 50% of collected premium — tastytrade practitioner research (2014) | `PROFIT_TAKE_PCT = 0.50` in `paper_trading.py` |
| 20% stop loss | Sell assigned shares if price drops >20% below put strike | `STOP_LOSS_PCT = 0.20` in `config.py` |

---

## Entry filters

| Filter | Value | Rationale |
|--------|-------|-----------|
| IVR | ≥ 40 | Only sell expensive options relative to their own history |
| Delta | 0.25 – 0.35 | ~70% probability OTM at expiry |
| DTE | 21 – 45 days | Steepest theta decay window |
| Stock price | $20 – $500 | Avoids penny stocks and single-contract capital overruns |
| Open interest | ≥ 500 | Liquid market |
| Volume | ≥ 50 | Active strikes only |
| Spread | ≤ 20% of mid | Avoids wide illiquid contracts |
| Earnings | > 7 days away | No earnings exposure |

Leveraged ETFs and volatility products (`UVXY`, `TQQQ`, `VXX`, etc.) are blocklisted — IVR signals are structurally meaningless on decay instruments.

---

## Scoring formula

```python
score = IVR × 0.6 + annualised_put_yield × 0.4

put_yield     = (bid / strike) × 100
annualised    = put_yield × (365 / DTE)
```

IVR weighted 60% (timing signal) over yield 40% (magnitude). The split was chosen empirically during backtesting.

---

## Capital allocation

| Parameter | Value |
|-----------|-------|
| Starting capital | $200,000 |
| Max positions | 5 |
| Position size | 20% / slot ($40,000) |
| Cash buffer | ≥ 10% free |
| Monthly target | 1% ($2,000) |

---

## References

### Volatility risk premium (the structural edge)

1. **Carr, P., & Wu, L. (2009).** Variance Risk Premiums. *Review of Financial Studies*, 22(3), 1311–1341.
   DOI: [10.1093/rfs/hhn038](https://doi.org/10.1093/rfs/hhn038)
   — Foundational paper establishing that variance risk premiums are large, negative, and persistent across equity indices.

2. **Bakshi, G., & Kapadia, N. (2003).** Delta-Hedged Gains and the Negative Market Volatility Risk Premium. *Review of Financial Studies*, 16(2), 527–566.
   DOI: [10.1093/rfs/hhg002](https://doi.org/10.1093/rfs/hhg002)
   — Shows delta-hedged option positions earn negative returns, confirming implied vol consistently exceeds realised vol.

3. **Bollerslev, T., Tauchen, G., & Zhou, H. (2009).** Expected Stock Returns and Variance Risk Premia. *Review of Financial Studies*, 22(11), 4463–4492.
   DOI: [10.1093/rfs/hhp008](https://doi.org/10.1093/rfs/hhp008)
   — Links the variance risk premium to equity risk premia and business cycle variation.

### Expected returns from selling puts

4. **Coval, J. D., & Shumway, T. (2001).** Expected Option Returns. *The Journal of Finance*, 56(3), 983–1009.
   DOI: [10.1111/0022-1082.00352](https://doi.org/10.1111/0022-1082.00352)
   — Empirically demonstrates that short put positions earn significantly positive expected returns, inconsistent with standard asset pricing models.

### Institutional benchmark

5. **Cboe Global Markets.** *Cboe S&P 500 PutWrite Index (PUT) — Methodology.*
   [CBOE PUT Index dashboard](https://www.cboe.com/us/indices/dashboard/PUT/) · [Methodology PDF](https://cdn.cboe.com/api/global/us_indices/governance/Cboe_SP_500_PutWrite_Indices_Methodology.pdf)
   — The institutional benchmark for systematic cash-secured put writing on the S&P 500.

6. **Whaley, R. E. (2002).** Return and Risk of CBOE Buy Write Monthly Index. *The Journal of Derivatives*, 10(2), 35–42.
   DOI: [10.3905/jod.2002.319194](https://doi.org/10.3905/jod.2002.319194)
   — Analyses the risk/return of systematic option writing, including the tradeoffs of different strike selection approaches.
