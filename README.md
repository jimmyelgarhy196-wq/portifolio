# EGX ALPHA

**A personal, hedge-fund-style AI research and portfolio-management terminal for Egyptian Exchange (EGX) listed equities.**

> ### ⚠ Paper trading and research only
> This system does **not** connect to a broker and does **not** place trades. Every
> position it manages is simulated. It is a research tool, not investment advice.

---

## What it does

EGX ALPHA ingests EGX market data, financial statements, news and official disclosures,
computes fundamental / technical / quantitative scores from that data, has AI analyst
agents argue both sides of each idea, writes a durable investment thesis, sizes a paper
position against explicit risk limits, and reports weekly like an investment committee.

Nine analytical layers:

| Layer | What it produces |
|---|---|
| Fundamental analysis | Growth, profitability, balance-sheet, cash-flow and valuation ratios |
| Technical analysis | 15 indicators, 10 signal detectors, trend/momentum state |
| Quantitative analysis | Cross-sectional factor z-scores across the EGX universe |
| News & events | Corporate actions, disclosures, earnings, sentiment |
| Risk | Volatility, beta, drawdown, concentration, correlation, liquidity, gap risk |
| Portfolio construction | Conviction- and volatility-aware sizing under hard limits |
| Backtesting | Point-in-time simulation with structural look-ahead prevention |
| AI research agents | Fundamental, Technical, Event, Bear analysts and a Portfolio Manager |
| Weekly committee report | Ten-section investment committee memo, stored and diffable |

---

## The two rules this system is built around

**1. It never invents a number.**
Every stored datum carries `source`, `retrieved_at`, `data_period` and `confidence`.
A missing input becomes `UNAVAILABLE` — a first-class value that propagates through
arithmetic instead of silently becoming zero. Division by zero returns `UNAVAILABLE`,
not infinity. Growth from a negative base returns `UNAVAILABLE`, not a misleading
percentage. Stale data reduces recommendation confidence and says so on screen.

**2. The AI never produces a score.**
Every score is deterministic Python over stored data, returned with its full
decomposition — inputs, normalisation, weight, contribution. The LLM writes narrative
around numbers it is handed, and must tag every statement `FACT`, `CALCULATION`,
`INFERENCE`, `OPINION` or `UNKNOWN`. Output containing a number absent from its evidence
pack is rejected. **Without an API key the system still works end to end**, using a
deterministic narrative engine.

---

## Quick start

```bash
git clone <this-repo> && cd portifolio

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # every credential is optional

python scripts/init_db.py     # create schema + load the EGX universe
python scripts/run_server.py  # http://127.0.0.1:8000
```

The terminal opens with an empty portfolio and no market data — because none has been
ingested yet. That is the honest default. See **Getting real data in** below.

### Trying it without any data source

To explore the interface before wiring up a provider:

```bash
EGX_ALLOW_SYNTHETIC_DATA=true python scripts/seed_demo.py
EGX_ALLOW_SYNTHETIC_DATA=true python scripts/run_server.py
```

This loads a **fictional** dataset. Every row is stamped `SYNTHETIC_DEMO`, a permanent
red banner sits across every page, and reports refuse to generate without an explicit
acknowledgement flag. It exists so the system can be demonstrated and tested offline.
**Never act on anything it shows you.**

---

## Getting real data in

Three routes, in order of reliability:

### 1. CSV import — always works, no credentials
Drop exported files into `data/manual/` and run `python scripts/ingest.py --provider csv`.
Formats are documented in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md). This is the
recommended path for fundamentals, since free APIs cover EGX financial statements poorly.

```
data/manual/
├── prices/COMI.csv           date,open,high,low,close,adjusted_close,volume
├── fundamentals/COMI.csv     period,period_type,period_end,available_from,revenue,...
├── news.csv                  ticker,title,source,url,publication_date,summary
└── disclosures.csv           ticker,title,date,type,url,summary
```

### 2. Yahoo Finance — prices only, no credentials
EGX tickers carry a `.CA` suffix (`COMI.CA`, `HRHO.CA`); the EGX30 index is `^CCSI`.

```bash
python scripts/ingest.py --provider yahoo --dataset prices --universe egx30
```

### 3. A commercial provider
Implement the interface in `backend/data/providers/base.py` and register it. Slots for
Refinitiv, Bloomberg and aggregator keys already exist in `.env.example`.

> **Network note.** The environment this project was built in blocks outbound access to
> all financial-data hosts, so live ingestion could not be exercised during development.
> The HTTP providers are written against the real endpoints and are covered by
> fixture-based tests; run `python scripts/ingest.py --dry-run` on your own machine to
> confirm connectivity.

---

## Using the terminal

| Page | Purpose |
|---|---|
| **Dashboard** | Portfolio value, P&L, alpha vs EGX30, top opportunities, risk summary |
| **Portfolio** | Positions, weights, P&L, thesis status per holding |
| **Opportunities** | Ranked universe scan with configurable filters |
| **Stocks** | Per-name page: chart, fundamentals, valuation, AI research, recommendation, sources |
| **Watchlist** | Core / swing / short / special-situation lists with notes |
| **Research** | Run agents on a name and read the tagged output |
| **Theses** | Every thesis, its versions, and what changed week over week |
| **Backtesting** | Run and compare strategies with full metrics |
| **Paper Trading** | Execute simulated trades from recommendations |
| **Risk** | Exposure, concentration, drawdown, limit breaches |
| **Reports** | Weekly committee report history, search, compare, export |
| **Model Evaluation** | How accurate the system's own past calls have been |
| **Settings** | Capital, risk limits, score weights, providers, schedule |

### Command line

```bash
python scripts/init_db.py                     # create schema, load universe
python scripts/ingest.py --dataset all        # run ingestion
python scripts/research.py --ticker COMI      # run the agent pipeline on one name
python scripts/backtest.py --strategy fundamental_long --start 2020-01-01
python scripts/weekly_run.py                  # full weekly pipeline + report
python scripts/run_server.py                  # start the terminal

python scripts/ingest.py --dry-run            # check which providers are reachable
python scripts/backtest.py --compare          # run every strategy over identical conditions
python scripts/refresh_universe.py --from-csv egx30.csv   # reconcile against official EGX lists
python scripts/gen_schema.py > database/SCHEMA.md         # regenerate schema docs
```

---

## Scoring

**EGX ALPHA score (0–100)** = Fundamental 30% · Technical 20% · Quant 15% ·
Catalysts 10% · Quality 10% · Risk 10% · Sentiment 5%

**Fundamental (0–100)** = Valuation 25% · Quality 20% · Growth 15% · Profitability 15% ·
Balance Sheet 10% · Cash Flow 10% · Catalysts 5%

**Quant factors** = Momentum 25% · Value 20% · Quality 20% · Growth 15% · Liquidity 10% ·
Volatility 10%

All weights live in `config/weights.yaml` and are editable in Settings. Every score comes
back with its decomposition — you can always see exactly which inputs produced it and how
much each contributed. Sub-scores computed from insufficient data are flagged
low-confidence and their weight is redistributed rather than assumed to be average.

---

## Strategies

- **Core Fundamental Long** (6–36+ months) — undervaluation, quality, balance sheet, cash flow
- **Technical / Swing** (days–months) — breakouts, momentum, reversals, volume confirmation
- **Special Situations** (event-driven) — M&A, restructuring, buybacks, capital actions
- **Bearish / Short** — **paper short positions only**, never executed

---

## Backtesting and look-ahead bias

The backtester cannot see the future, because the data layer refuses to serve it.
`PointInTimeDataView` is constructed with an `as_of` date and filters every query by it.
Financial statements additionally respect `available_from` — a Q4 result stays invisible
until the date it was actually published, not the date the period ended. The test suite
includes a strategy that deliberately attempts to read ahead and asserts it gets nothing.

Metrics: total return, CAGR, volatility, Sharpe, Sortino, max drawdown, Calmar, win rate,
average winner/loser, profit factor, alpha, beta, turnover — all against EGX30.

---

## Testing

```bash
pytest -q                          # 338 tests
pytest tests/test_lookahead.py -v  # the ones that matter most
```

Covers financial calculations, technical indicators, scoring, position sizing, portfolio
allocation, risk limits, backtest mechanics and metrics, ingestion edge cases
(duplicates, gaps, holidays, malformed payloads, provider failures), agent output
validation, report generation and every API endpoint — with dedicated suites for
look-ahead bias, division by zero, missing data and period misalignment.

Tests run against an in-memory database and make no network calls, so they are safe to
run anywhere.

---

## Architecture

```
backend/
├── core/          config, logging, database, data-quality primitives
├── data/          provider interfaces + implementations, ingestion, universe
├── analytics/     fundamental, technical, quant, scoring engines
├── research/      AI agents, evidence packs, thesis engine, memory
├── portfolio/     paper trading, sizing, risk, attribution
├── backtesting/   point-in-time views, engine, metrics, strategies
├── reports/       weekly investment committee report
├── jobs/          scheduler and weekly pipeline
└── api/           FastAPI routers and page routes
frontend/          Jinja2 templates, terminal CSS, dependency-free canvas charts
```

Dependencies flow one way: `api → {analytics, research, portfolio, backtesting, reports}
→ data → core`. Analytics performs no I/O. Research computes no scores. The API holds no
business logic.

Stack: Python 3.11 · FastAPI · SQLAlchemy 2.0 · SQLite (PostgreSQL-ready) · pandas/numpy ·
Jinja2 · no frontend build step, no external JS dependencies.

| Document | Contents |
|---|---|
| [`PROJECT_PLAN.md`](PROJECT_PLAN.md) | Architecture decisions and the rationale for each |
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | CSV formats, provider contract, how to add a source |
| [`database/SCHEMA.md`](database/SCHEMA.md) | Every table and column, generated from the models |

---

## Security

Secrets come from environment variables only. `.env` is git-ignored; `.env.example`
documents every variable and commits no values. Notifications stay off unless explicitly
configured. `EGX_LIVE_TRADING_ENABLED` exists as an auditable gate that defaults to
`false` and is wired to no execution path — there is no broker integration in this
codebase.

---

## Disclaimer

EGX ALPHA is a personal research tool. Its output is generated by software and language
models and may be incomplete or wrong. It is not investment advice, and nothing in it is
a recommendation to buy or sell any security. Verify every figure against primary sources
before acting on it.
