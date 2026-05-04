# Options Trader — Wheel Strategy

An automated wheel strategy system for selling cash-secured puts on S&P 500 stocks, validated against six years of real historical options data and deployed on a Raspberry Pi for daily execution.

## What it does

Every trading day:
- **8:00 AM ET** — scans 85 tickers for elevated implied volatility, scores candidates by IV Rank and annualised yield, emails a ranked shortlist
- **4:30 PM ET** — manages open paper positions (profit takes, expiries, stop losses), opens new positions from the screener, emails an end-of-day portfolio snapshot

## Strategy in one sentence

Sell 30-delta cash-secured puts on liquid S&P 500 stocks only when IV Rank ≥ 40, collect premium, close at 50% profit, repeat.

## Validated results (2020–2026, ORATS data)

| Metric | Value |
|--------|-------|
| Pre-tax annual return | 26.2% |
| After-tax annual return (37% ordinary) | 19.5% |
| SPY after-tax (15% LTCG) | 13.1% |
| Outperformance vs SPY | +6.4 pp/yr |
| Walk-forward retention ratio | 163% (robust) |
| Sharpe ratio (full period) | 1.58 |

## Documentation

| Doc | Contents |
|-----|----------|
| [Data Sources](docs/01_data_sources.md) | ORATS backtest data, yfinance live data, IV history store |
| [Strategy](docs/02_strategy.md) | Wheel mechanics, all terms defined, scoring formula, parameter rationale |
| [Backtest Results](docs/03_backtest_results.md) | Walk-forward validation, after-tax analysis, stop-loss clustering |
| [Paper Trading](docs/04_paper_trading.md) | Engine design, current positions, go-live criteria |
| [Technical Design](docs/05_technical_design.md) | System architecture, data flow, mermaid diagrams |

## Quick start

### Mac / local

```bash
git clone https://github.com/cpatchava/options-trader.git
cd options-trader
pip install -r requirements.txt
cp .env.example .env          # add Gmail credentials
python daily_report.py        # run screener
python paper_trading.py       # run paper trading engine
```

### Raspberry Pi

```bash
git clone https://github.com/cpatchava/options-trader.git ~/code/options-trader
bash ~/code/options-trader/setup_pi.sh
# add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to ~/.env
```

The setup script sets the timezone to America/New_York, creates a Python venv, installs all dependencies, and installs the crontab.

## Repository structure

```
options-trader/
├── config.py               # all tuneable parameters in one place
├── screener.py             # daily options screener
├── daily_report.py         # morning email report
├── paper_trading.py        # end-of-day paper trading engine
├── utils/
│   ├── bsm.py              # Black-Scholes-Merton pricing and Greeks
│   ├── data_fetcher.py     # yfinance price helpers
│   └── iv_history.py       # 52-week IV history store
├── run_report.sh           # cron wrapper — morning
├── run_paper_trading.sh    # cron wrapper — evening
├── setup_pi.sh             # one-shot Raspberry Pi setup
├── data/
│   ├── iv_history.csv      # accumulated daily IV readings (not in git)
│   ├── paper_trades.csv    # paper trade ledger (not in git)
│   └── paper_log.csv       # daily portfolio snapshots (not in git)
├── docs/                   # extended documentation
└── backtest_orats.py       # historical backtest (research, not live)
```

## Capital allocation

| Parameter | Value |
|-----------|-------|
| Starting capital | $200,000 |
| Max positions | 5 |
| Position size | 20% of portfolio ($40,000/slot) |
| Cash buffer | 10% minimum free cash |
| Monthly target | 1% ($2,000/month) |
