# Strategy — The Wheel

The [Wheel Strategy](https://en.wikipedia.org/wiki/Wheel_strategy) is a systematic options income approach: sell cash-secured puts → if assigned, sell covered calls → repeat. The economic basis is the [volatility risk premium](https://en.wikipedia.org/wiki/Volatility_risk_premium) — implied volatility persistently exceeds realised volatility, so option sellers collect a structural edge over time.

Reference implementations: [tastytrade — The Wheel](https://tastytrade.com/learn/options/the-wheel), [Options Alpha](https://optionsalpha.com/blog/wheel-options-strategy/).

---

## Key terms

| Term | Definition | Source |
|------|-----------|--------|
| [Implied Volatility (IV)](https://en.wikipedia.org/wiki/Implied_volatility) | Market's annualised forward vol estimate, derived from option prices | yfinance `impliedVolatility`; ORATS `smoothSmvVol` |
| [IV Rank (IVR)](https://en.wikipedia.org/wiki/Implied_volatility#Implied_volatility_rank) | Percentile of today's IV vs 52-week range: `(IV - low) / (high - low) × 100` | `utils/iv_history.get_ivr()` from rolling `iv_history.csv` |
| [Delta (Δ)](https://en.wikipedia.org/wiki/Greeks_(finance)#Delta) | $/$ sensitivity of option price to stock move; also ≈ probability of expiring ITM | BSM via `utils/bsm.delta()`; ORATS `delta` in backtest |
| [Theta (Θ)](https://en.wikipedia.org/wiki/Greeks_(finance)#Theta) | Daily time decay in option value — positive for sellers | Informational only; not directly used in code |
| [DTE](https://en.wikipedia.org/wiki/Expiration_(options)) | Calendar days until expiry | Computed from expiry date |
| [Cash-secured put](https://en.wikipedia.org/wiki/Put_option#Cash-secured_put) | Short put backed by full cash collateral (strike × 100) | Core position type |
| [Assignment](https://en.wikipedia.org/wiki/Option_assignment) | Put exercised at expiry → we buy 100 shares at strike | Managed in `paper_trading.manage_positions()` |
| [Covered call](https://en.wikipedia.org/wiki/Covered_call) | Short call against held shares, strike ≥ cost basis | Second leg of the wheel |
| 50% profit take | Close when bid ≤ 50% of collected premium — validated at tastytrade | `PROFIT_TAKE_PCT = 0.50` in `paper_trading.py` |
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
