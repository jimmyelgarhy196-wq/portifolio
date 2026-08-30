# EGX ALPHA — Project Plan

**A personal, research-focused AI investment platform for Egyptian Exchange (EGX) listed equities.**

Status: Phases 1–5 implemented and verified. 338 tests passing. Paper-trading / research only.

---

## 1. Purpose and Non-Goals

### Purpose
Build a hedge-fund-style research and portfolio-management system that surfaces EGX
opportunities through fundamental, technical, quantitative, event, and risk analysis,
combines them into an explainable score, expresses them as durable investment theses,
manages a paper portfolio against those theses, and reports weekly like an investment
committee.

### Explicit Non-Goals
- **No live trading.** No broker integration exists. `EGX_LIVE_TRADING_ENABLED` is an
  auditable gate that defaults to `false` and is not wired to any execution path.
- **No fabricated data.** The system never invents a price, ratio, news item, or
  disclosure. Missing data is represented as `UNAVAILABLE` and propagates as reduced
  confidence, not as a guess.
- **No opaque AI scores.** Every score is computed by deterministic Python from stored
  data. The LLM writes narrative around numbers it is given; it never produces a number
  that enters the scoring system.

---

## 2. Repository Inspection (starting point)

The repository was **empty** — no code, no commits, no tech stack. This is a greenfield
build, so the stack was chosen from first principles rather than inherited.

---

## 3. Architecture Decisions

Each decision below was made unilaterally per the brief, and is recorded with its rationale.

### AD-1 — Backend language: **Python 3.11**
The entire problem domain (time series, ratio analysis, factor models, backtesting,
portfolio optimisation) is native to the Python numerical stack. `pandas` and `numpy`
carry the quantitative core. Rejected: TypeScript backend (would require reimplementing
or binding numerical primitives).

### AD-2 — Web framework: **FastAPI + Uvicorn**
Async HTTP for concurrent provider fetches, Pydantic validation on every boundary, and a
free OpenAPI spec at `/api/docs` that doubles as API documentation.

### AD-3 — Database: **SQLite by default, PostgreSQL-compatible**
Accessed exclusively through SQLAlchemy 2.0 ORM with portable column types, so
`EGX_DATABASE_URL` switches to PostgreSQL with no code change. SQLite keeps a personal
research system to zero infrastructure. Rejected: a time-series database — EGX has ~250
liquid names and daily bars; that is small data and joins matter more than write
throughput.

### AD-4 — Frontend: **Server-rendered Jinja2 + vanilla JS, no build step**
A React/Vite SPA would add a Node toolchain, a build pipeline, and a second dependency
tree to maintain for a single-user research terminal. Server-rendered pages with
progressive enhancement give a faster path to a working product and one deployable
artifact. Charts use a small hand-written canvas renderer (`frontend/static/js/charts.js`)
with **zero external JS dependencies**, so the terminal works fully offline and behind
restrictive networks. Aesthetic: dark institutional terminal — original design, not a
copy of any proprietary UI.

### AD-5 — Data access: **provider abstraction, real sources, no fabrication**
Four interfaces — `MarketDataProvider`, `FundamentalDataProvider`, `NewsProvider`,
`DisclosureProvider` — each with multiple implementations selected by config and tried
in priority order:

| Provider | Domain | Credentials | Notes |
|---|---|---|---|
| `CsvFileProvider` | market, fundamentals, news, disclosures | none | Guaranteed-working path. You drop real exported data into `data/manual/`. |
| `YahooFinanceProvider` | market | none | EGX tickers carry the `.CA` suffix (`COMI.CA`). Real HTTP client. |
| `EgxDisclosureProvider` | disclosures | none | Official EGX disclosure feed. |
| `RssNewsProvider` | news | none | Configurable RSS/Atom feeds. |
| `SyntheticProvider` | market, fundamentals | none | **Off by default.** Fictional data for offline demo/testing only. |
| `NullProvider` | all | none | Returns `UNAVAILABLE`. The honest default when nothing is configured. |

**On the synthetic provider.** The build environment for this project blocks outbound
access to every financial data host, so live ingestion could not be exercised here. Rather
than ship an application that cannot be demonstrated, there is a synthetic dataset that is
(a) disabled unless `EGX_ALLOW_SYNTHETIC_DATA=true`, (b) stamped `source=SYNTHETIC_DEMO`
on every row in the database, (c) surfaced as a permanent red banner across the entire UI,
and (d) refused by the report generator unless explicitly acknowledged. It is a test
fixture, not a data source.

### AD-6 — AI: **evidence-bound agents with a deterministic fallback**
Agents never see the open internet or free-form recall. Each receives an *evidence pack*
built from the database — computed metrics with their sources and retrieval timestamps —
and must tag every statement `FACT`, `CALCULATION`, `INFERENCE`, `OPINION`, or `UNKNOWN`.
Output is validated; untagged or numerically-inconsistent output is rejected. With no
`ANTHROPIC_API_KEY`, a deterministic template engine composes the same theses from the
same metrics, so **the system is fully functional without an LLM**.

### AD-7 — Scoring: **deterministic, decomposable, configurable**
Every score is a weighted sum of sub-scores, each traceable to named inputs. The API
returns the full decomposition — inputs, normalisation, weight, contribution — alongside
the number, so any score can be audited to its source rows.

### AD-8 — Look-ahead bias: **structural prevention, not discipline**
The backtester cannot see the future because the data access layer refuses to return it.
`PointInTimeDataView` is constructed with an `as_of` date; every query it serves is
filtered by that date, and fundamentals additionally respect a reporting lag
(`available_from`) so a Q4 result is invisible until it was actually published. Tests
assert that a deliberately look-ahead strategy is starved of data.

---

## 4. Module Map

```
backend/
├── core/          config, logging, database session, data-quality primitives
├── data/          provider interfaces + implementations, ingestion, universe
├── analytics/     fundamental, technical, quant, scoring engines
├── research/      AI agents, evidence packs, thesis engine, memory
├── portfolio/     paper trading, position sizing, risk engine, attribution
├── backtesting/   point-in-time views, engine, metrics, strategies
├── reports/       weekly investment committee report generation
├── jobs/          scheduler and the weekly pipeline
└── api/           FastAPI routers + page routes
frontend/          Jinja2 templates, terminal CSS, canvas charting
database/          SQLite file + schema documentation
tests/             pytest suite
scripts/           CLI entry points (init db, ingest, research, backtest, report)
config/            default YAML configuration (weights, limits, universe)
```

### Layer boundaries
Data ingestion, quantitative calculation, AI reasoning, portfolio management, UI, and
reporting are separate modules. Dependencies flow one way:

```
api → {analytics, research, portfolio, backtesting, reports} → data → core
```

Analytics never performs I/O against providers. Research never computes a score. The API
never contains business logic.

---

## 5. Data Model

Tables: `companies`, `price_history`, `financial_statements`, `valuation_snapshots`,
`news`, `disclosures`, `research_theses`, `thesis_versions`, `portfolios`,
`positions`, `trades`, `recommendations`, `score_history`, `watchlist_items`, `alerts`,
`reports`, `backtest_runs`, `data_quality_log`, `settings`.

Every ingested row carries `source`, `retrieved_at`, `data_period`, and `confidence`.
`recommendations` and `score_history` are append-only so the system can grade its own
past predictions.

Full column-level schema: `database/SCHEMA.md`.

---

## 6. Scoring Model

**Fundamental (0–100)** — Valuation 25, Quality 20, Growth 15, Profitability 15,
Balance Sheet 10, Cash Flow 10, Catalysts 5.

**Technical (0–100)** — trend, momentum, RSI positioning, MACD, volume confirmation,
relative strength, volatility posture, support/resistance location.

**Quant (0–100)** — cross-sectional factor z-scores over the EGX universe:
Momentum 25, Value 20, Quality 20, Growth 15, Liquidity 10, Volatility 10.

**EGX ALPHA master score (0–100)** — Fundamental 30, Technical 20, Quant 15,
Catalysts 10, Quality 10, Risk 10, Sentiment 5.

All weights live in `config/weights.yaml` and are editable from Settings.

---

## 7. Strategies

| Strategy | Horizon | Core signal |
|---|---|---|
| Core Fundamental Long | 6–36+ months | Undervaluation + quality + balance sheet + FCF |
| Technical / Swing | days–months | Breakout, momentum, trend reversal, volume confirmation |
| Special Situations | event-driven | M&A, restructuring, buybacks, capital actions, contracts |
| Bearish / Short (paper only) | variable | Valuation extremes, earnings decline, leverage, breakdowns |

Bearish theses are **paper short positions only** and are labelled as such throughout.

---

## 8. Delivery Phases

- **Phase 1 — Foundation.** DB, universe, providers, market data engine, fundamental and
  technical analysis, stock pages, dashboard.
- **Phase 2 — AI.** Fundamental / Technical / Event / Bear analysts, Portfolio Manager,
  thesis system with weekly versioning.
- **Phase 3 — Portfolio.** Paper trading, conviction- and volatility-aware position
  sizing, risk engine with limit breaches, performance attribution.
- **Phase 4 — Quant.** Backtesting with look-ahead prevention, factor models, strategy
  comparison.
- **Phase 5 — Automation.** Weekly research pipeline, investment committee report, alerts,
  historical thesis evaluation, model evaluation page.

After each phase: run the app, run the tests, fix failures, verify UI and DB, document.

---

## 9. Anti-Hallucination Rules (enforced in code)

1. Statements are tagged `FACT` / `CALCULATION` / `INFERENCE` / `OPINION` / `UNKNOWN`.
2. Numbers cited by an agent are cross-checked against the evidence pack; a number that
   does not appear there fails validation and the response is rejected.
3. Missing inputs yield `UNAVAILABLE`, never a substituted value or a default.
4. Every metric carries `source` and `retrieved_at`; staleness reduces confidence and is
   shown in the UI.
5. Scores are computed in Python. The LLM cannot emit one.
6. Sub-scores computed from insufficient data are marked low-confidence and their weight
   is redistributed rather than assumed average.

---

## 10. Testing Strategy

`tests/` covers financial calculations, technical indicators, score computation, position
sizing, portfolio allocation, risk limits, backtest mechanics and metrics, ingestion
(duplicates, gaps, holidays, bad payloads, provider failures), agent output validation,
report generation, and API endpoints — with dedicated suites for look-ahead bias,
division by zero, missing data, and period misalignment. 338 tests, all passing, against
an in-memory database with no network calls.

The suite earned its place: it caught four real correctness bugs during development —
winsorising that failed to clip anything on small samples, an explicitly-empty provider
chain silently falling back to network providers, a backtest position cap undone by
renormalisation (one candidate allocated 95% against a 20% limit), and a persistence
call writing to a column that did not exist.

---

## 11. Operational Notes

- Structured logging for data updates, provider failures, research runs, portfolio
  changes, report generation, backtests, and errors.
- Secrets only via environment variables; `.env` is git-ignored; `.env.example` documents
  every variable.
- Notifications are disabled unless explicitly configured.
