# Technical Design

## System architecture

```mermaid
graph TD
    Pi[Raspberry Pi<br/>cron scheduler]

    Pi -->|10:00 ET Mon-Fri| MR[run_report.sh]
    Pi -->|16:30 ET Mon-Fri| PT[run_paper_trading.sh]

    MR --> DR[daily_report.py]
    PT --> PTE[paper_trading.py]

    DR --> SC[screener.py]
    PTE --> SC

    SC --> IVH[utils/iv_history.py]
    SC --> YF[yfinance]
    SC --> BSM[utils/bsm.py]

    IVH --> IVCSV[(iv_history.csv)]
    PTE --> CSV[(paper_trades.csv<br/>paper_log.csv)]

    DR -->|HTML email| Gmail[Gmail SMTP :465]
    PTE -->|HTML email| Gmail
    Gmail --> Inbox[Gmail Inbox]
```

---

## Morning screener flow

```mermaid
flowchart TD
    A[daily_report.py] --> B[iv_history.update WATCHLIST]
    B --> C{already recorded today?}
    C -->|yes| D[skip]
    C -->|no| E[yfinance: ATM put IV near 30 DTE]
    E --> F[append to iv_history.csv]

    F --> G[for each ticker]
    G --> H[get_ivr]
    H --> I{IVR >= 40?}
    I -->|no| J[skip]
    I -->|yes| K[price check $20-$500]
    K --> L[earnings check > 7d]
    L --> M[find delta 0.25-0.35 put on chain]
    M --> N[score = IVR×0.6 + ann_yield×0.4]
    N --> O[append to results]

    O --> P[sort, take top 8]
    P --> Q[send email]
```

---

## Paper trading engine flow

```mermaid
flowchart TD
    A[paper_trading.py] --> B{weekend?}
    B -->|yes| Z[exit]
    B -->|no| C[load paper_trades.csv]

    C --> D[for each open put]
    D --> E{DTE <= 0?}
    E -->|yes, ITM| F[ASSIGNED → share position]
    E -->|yes, OTM| G[EXPIRED → keep premium]
    E -->|no| H{bid <= 50% of premium?}
    H -->|yes| I[50% PROFIT TAKE]
    H -->|no| J[hold]

    C --> K[for each share position]
    K --> L{price <= strike × 0.80?}
    L -->|yes| M[STOP LOSS]
    L -->|no| N[hold]

    I & F & G & M --> O[run screener]
    J & N --> O

    O --> P{free slots?}
    P -->|no| Q[skip]
    P -->|yes| R[open new puts from candidates]

    R --> S[save paper_trades.csv]
    Q --> S
    S --> T[append paper_log.csv]
    T --> U[send email]
```

---

## File structure

```
options-trader/
├── config.py                   # all parameters — single source of truth
├── screener.py                 # → List[Dict] ranked candidates
├── daily_report.py             # morning email
├── paper_trading.py            # end-of-day engine + evening email
├── utils/
│   ├── bsm.py                  # price(), delta(), strike_for_delta()
│   ├── data_fetcher.py         # get_prices(), historical_volatility()
│   └── iv_history.py           # update(), get_ivr(), bootstrap_from_orats()
├── data/                       # runtime data, not in git
│   ├── iv_history.csv
│   ├── paper_trades.csv
│   └── paper_log.csv
├── run_report.sh
├── run_paper_trading.sh
└── setup_pi.sh
```

---

## Configuration reference

All tuneable parameters in `config.py`:

| Parameter | Value |
|-----------|-------|
| `DELTA_LOW / HIGH` | 0.25 / 0.35 |
| `DTE_MIN / MAX` | 21 / 45 |
| `MIN_IVR` | 40 |
| `MAX_POSITIONS` | 5 |
| `POSITION_SIZE_PCT` | 20% |
| `STOP_LOSS_PCT` | 20% |
| `PROFIT_TAKE_PCT` | 50% |
| `MIN_OPTIONS_OI` | 500 |
| `MAX_SPREAD_PCT` | 20% |
| `EARNINGS_BUFFER_DAYS` | 7 |
| `COMMISSION_PER_CONTRACT` | $0.65 |
| `RISK_FREE_RATE` | 4.5% |
