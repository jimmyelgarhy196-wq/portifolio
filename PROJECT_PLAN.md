# GMG Investment Intelligence — Project Plan

**A subscription research and analytics platform for Egyptian Exchange (EGX) listed
equities, by GMG AI Solutions.**

Status: research engines and the subscription platform implemented and verified.
488 tests passing. Research and information only — see [`LEGAL.md`](LEGAL.md).

---

## 0. How this document relates to the product

The research engines described below (Phases 1–5) were built first as an internal
analytical stack. They now sit behind **GMG Investment Intelligence**, the
customer-facing subscription product: accounts, entitlement, a market dashboard,
a ten-tab stock page, valuation tools, a screener, watchlists, portfolio tracking,
alerts and an admin panel. The internal research terminal remains, mounted at
`/terminal` for GMG staff only.

See [`README.md`](README.md) for what subscribers get and
[`DEPLOYMENT.md`](DEPLOYMENT.md) for running it.

---

## 1. Purpose and Non-Goals

### Purpose
Provide EGX investors with institutional-style research: fundamental, technical,
quantitative, event and risk analysis combined into an explainable score, expressed
as durable investment theses, with valuation tools, screening, tracking and a weekly
report — and with the source, age and limits of every number visible on screen.

### Explicit Non-Goals
- **No client money, no custody, no execution, no discretionary management.**
  GMG AI Solutions never holds a customer's money or securities, never places an
  order, and never manages a portfolio for anyone.
- **No personal investment advice.** Output is analysis, not a recommendation to
  any individual.
- **No claimed regulatory licence.** GMG is not registered or licensed by the FRA
  or any other regulator, and says so.
- **No live trading.** No broker integration exists. `EGX_LIVE_TRADING_ENABLED` is an
  auditable gate that defaults to `false` and is not wired to any execution path.
- **No fabricated data.** The system never invents a price, ratio, news item, or
  disclosure. Missing data is represented as `UNAVAILABLE` and propagates as reduced
  confidence, not as a guess.
- **No opaque AI scores.** Every score is computed by deterministic Python from stored
  data. The LLM writes narrative around numbers it is given; it never produces a number
  that enters the scoring system.

---

## 2. Repository inspection

**First build:** the repository was empty — no code, no commits, no tech stack — so the
stack was chosen from first principles rather than inherited.

**SaaS build:** the existing codebase was inspected before anything was written. The
research engines, data-quality primitives, provenance model, scoring framework and
point-in-time access were kept and extended rather than replaced. What was added sits
on top: a quote layer with provenance, market aggregation, accounts, entitlement,
billing abstraction, the customer-facing UI, and a design system. The original terminal
UI was retained and moved behind `/terminal`.

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

**GMG master score (0–100)** — Fundamental 30, Technical 20, Quant 15,
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

---

# Part II — The subscription platform

Built on top of the research engines above, without replacing them.

## Architecture decisions

| Decision | Rationale |
|---|---|
| **Quote provider chain: licensed → stored → demo** | Each provider declares `is_demo` and `delayed_minutes`, which travel with every quote into the database and out to the UI. The licensed slot **raises rather than falling back** — a silent downgrade is how generated prices end up behind a "live" label. |
| **Index levels are never synthesised** | An official EGX index level is shown only when a licensed feed supplies it. Otherwise the figure is a constituent composite, labelled as computed by GMG, with its coverage count. |
| **Demo contamination propagates** | If any input quote is demonstration data, every aggregate built from it is flagged demo. Fabricated inputs cannot launder into a clean-looking total. |
| **Entitlement decided server-side, before rendering** | `require_subscriber` runs as a FastAPI dependency; a locked template is substituted for the premium one. Premium markup is never sent to the browser, so editing JavaScript unlocks nothing. |
| **Status alone never grants access** | `Subscription.is_entitled()` checks the period end as well as the status. An `ACTIVE` row past its period is denied; a `CANCELLED` row inside its paid period is honoured. |
| **No demo payment gateway** | A provider that flipped payments to `SUCCEEDED` would leave a database indistinguishable from a real one. With no gateway, checkout records intent, says no card was charged, and an administrator confirms — audited with their identity. |
| **Rating derived by rules, not written by a model** | The rating, confidence, horizon and category come from fixed thresholds over the scores, so they are reproducible and cannot be hallucinated. A rating is withheld below 45% score coverage. |
| **Valuation withholds on disagreement** | When contributing methods differ by more than 2.5×, no single fair value is published; the range and the disagreement are shown instead. |
| **Screener excludes on unknown values** | A company whose P/E is unknown is neither cheap nor expensive. It is excluded and counted, so a user can tell "none passed" from "we could not tell". |
| **Alerts never fire on demonstration data** | Emailing someone that their stock crossed a level, on a generated price, is the most damaging thing this system could do. |
| **Anonymous forms carry a CSRF token** | Derived from a short-lived HttpOnly cookie, so sign-in and sign-up are protected against login CSRF, not just post-authentication forms. |
| **The internal terminal is admin-only** | Backtesting, paper trading, risk and thesis tooling are GMG's own; they are not part of the subscription and are not exposed to subscribers. |

## Modules added

```
backend/market/          status.py (EGX session), quotes.py (providers + cache),
                         overview.py (indices, movers, breadth, search)
backend/accounts/        security.py (Argon2id, tokens, CSRF), service.py (lifecycle)
backend/billing/         subscriptions.py (entitlement), payments.py (provider abstraction)
backend/notify/          email_service.py (provider abstraction), user_alerts.py
backend/analytics/       valuation.py (DCF, multiples, blend), screener.py
backend/research/        rating.py (rating, confidence, horizon, category)
backend/api/             auth_deps.py, routes_auth, routes_billing, routes_gmg,
                         routes_workspace, routes_admin, routes_public
frontend/static/css/     gmg.css — the design system
frontend/static/js/      gmg.js, gmg-chart.js, gmg-stock.js
frontend/templates/gmg/  customer UI, seven legal documents, admin panel
```

## Data model added

16 tables: `users`, `user_sessions`, `one_time_tokens`, `login_attempts`,
`subscriptions`, `payments`, `user_watchlists`, `user_watchlist_items`,
`user_portfolios`, `user_positions`, `saved_screens`, `user_alerts`,
`audit_log`, `email_log`, `market_quotes`, `data_sources`.

They share the research schema's `Base`, so one `init_database()` creates everything.

## Verification

- **488 automated tests**, including 147 written for this platform: quote provenance,
  freshness labelling, aggregate contamination, access control at four privilege
  levels, CSRF, per-user data isolation, valuation refusals, rating withholding,
  screener exclusion and alert suppression.
- **A 22-step browser journey** — land, price, sign up, sign in, search, chart,
  premium tab, subscription, valuation, watchlist, portfolio, alert, screener,
  legal — passing 44/44 on desktop (1440px) and mobile (390px), with no JavaScript
  errors and no horizontal overflow.
