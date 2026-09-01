# GMG Investment Intelligence

**Investment Intelligence for the Egyptian Market** — a subscription research
and analytics platform for equities listed on the Egyptian Exchange (EGX), by
**GMG AI Solutions**.

---

## What this is — and what it is not

GMG Investment Intelligence provides market information, analytics and research
commentary. It is **information and research only**.

**GMG AI Solutions does not:**

- accept or hold client money
- hold securities or act as a custodian
- execute or route orders, or act as a broker
- manage portfolios for clients
- give personal investment advice
- guarantee, promise or forecast any return

GMG AI Solutions is **not licensed or registered** as a financial adviser,
portfolio manager, securities brokerage or investment fund by the Egyptian
Financial Regulatory Authority (FRA) or any other regulator, and makes no such
claim. See [`LEGAL.md`](LEGAL.md) for the outstanding legal-review work that
must be completed before the service is offered commercially.

---

## The rule the whole codebase is built around

**No fabricated number is ever presented as a real one.**

That single rule produces most of the architecture:

| Situation | What the platform does |
|---|---|
| A price is unavailable | Shows `N/A — data unavailable`, never `0.00` |
| A price is delayed | Shows `DELAYED` with the exact delay in minutes |
| A price is a stored close | Shows `END OF DAY` with the bar's true age |
| No real feed is connected | Shows `DEMO DATA — NOT REAL-TIME`, and never fires an alert |
| A ratio's denominator is zero | Reports it unavailable, never infinity |
| A score's inputs are too sparse | Withholds the score rather than defaulting it |
| A rating rests on thin coverage | Withholds the rating, and says why |
| Valuation methods disagree > 2.5× | Publishes the range and refuses a single figure |
| A multiple is physically implausible | Withholds it with an explanation (units guard) |
| A screener criterion can't be tested | Excludes the company and counts it as untested |

An index level is only ever shown as **official** when a licensed feed supplies
it. Otherwise the figure is a **constituent composite computed by GMG** and is
labelled as such, with the number of constituents it covers.

**TradingView is not a data source here.** It is not scraped, and no claim is
made that it offers an unrestricted real-time market-data API.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print('EGX_AUTH_SECRET=' + secrets.token_urlsafe(48))" >> .env

python scripts/init_db.py            # create the schema
python scripts/refresh_universe.py   # load the EGX universe
python scripts/run_server.py         # http://127.0.0.1:8000
```

The first account you create becomes the administrator.

### Exploring it with demonstration data

With no market-data licence connected there is nothing real to show, so a demo
dataset is provided. **Every figure it writes is fictional** and is stamped
`SYNTHETIC_DEMO` so the platform labels it `DEMO DATA — NOT REAL-TIME`
everywhere it appears.

```bash
EGX_ALLOW_SYNTHETIC_DATA=true python scripts/seed_gmg_demo.py --reset
python scripts/research.py            # compute scores across the universe
python scripts/run_server.py
```

Delete the database and ingest from a licensed provider before showing anything
to a customer.

---

## What subscribers get

| Area | Detail |
|---|---|
| **Market dashboard** | EGX 30 / 70 / 100 with constituent breadth, top gainers, top losers, most active by traded value, advancers vs decliners, total volume and market turnover |
| **Stock page** | Ten tabs: Overview, Chart, Fundamentals, Financials, Valuation, Technicals, AI Research, News & Disclosures, Peers & Sector, Data & Sources |
| **Charts** | Candlesticks with volume, 1M–MAX timeframes, SMA/EMA/Bollinger overlays, RSI and MACD panes, crosshair OHLC readout — drawn locally, no third-party charting service |
| **Fundamental score** | Valuation, Quality, Growth, Profitability, Balance sheet, Cash flow, **Dividend** and Catalysts, each with its inputs, weight and explanation |
| **Valuation** | Two-stage DCF you can re-run with your own assumptions, a discount-rate × terminal-growth sensitivity grid, and sector/own-history multiples |
| **AI research** | Rating, confidence, investment horizon and category (fundamental / technical / hybrid), derived by fixed rules and withheld when coverage is thin |
| **Screener** | 22 criteria across price, valuation, quality, growth, momentum and GMG scores, with saved screens |
| **Workspace** | Unlimited watchlists, portfolio tracking with P/L and weights, price / RSI / moving-average alerts by email |
| **Weekly report** | GMG EGX Intelligence, generated from stored data with its own provenance |

Pricing: **EGP 300 per month**, with a 7-day free trial.

---

## Architecture

```
Licensed market-data provider ─┐
Stored end-of-day bars ────────┼──► Quote service ──► Quote cache ──► Pages / JSON API
Demo provider (labelled) ──────┘         │
                                          └─ provenance travels with every quote:
                                             source · timestamp · delay · is_demo
```

```
backend/
  core/          settings, database, logging, data-quality primitives (UNAVAILABLE, safe_div)
  data/          ORM models (research + SaaS), providers, ingestion, universe
  market/        quote providers, session state, index/mover/breadth aggregation
  analytics/     fundamental, technical, quantitative, scoring, valuation, screener
  research/      evidence packs, AI agents, thesis engine, rating derivation
  portfolio/     sizing, paper trading, risk, attribution
  backtesting/   point-in-time data access, metrics, strategies, engine
  accounts/      password hashing, tokens, CSRF, registration, sessions
  billing/       subscription lifecycle, payment-provider abstraction
  notify/        email provider abstraction, user alerts
  api/           app factory, auth dependencies, page and JSON routes
frontend/
  static/css/    gmg.css (design system), terminal.css (internal terminal)
  static/js/     gmg.js (shell), gmg-chart.js (charting), gmg-stock.js
  templates/gmg/ customer-facing pages, legal documents, admin panel
```

### Security

- Passwords hashed with **Argon2id** (memory-hard); plaintext never logged or stored
- Session tokens stored only as **SHA-256 digests**; cookies HMAC-signed, `HttpOnly`, `SameSite=Lax`, `Secure` in production
- Changing a password bumps a **session epoch**, invalidating every existing session
- **CSRF tokens derived from the session secret** on every state-changing form
- Per-IP and per-account **rate limiting and lockout** on sign-in, sign-up and reset
- Sign-in and reset responses **never reveal whether an account exists**
- **Entitlement is decided server-side** before a premium template is rendered or a premium JSON payload is produced — editing frontend JavaScript unlocks nothing
- Every user-owned query is scoped by `user_id`; guessing another user's id returns 404
- `EGX_AUTH_SECRET` is **required in production**; the application refuses to start without it

### Payments

There is no card processing anywhere in this codebase and **no demo gateway**.
A provider that flipped payments to `SUCCEEDED` on its own would leave a
database indistinguishable from a real one. With no gateway connected, checkout
records intent, states plainly that no card has been charged, and an
administrator confirms the out-of-band payment — an action written to the audit
log with their identity and the reference they entered.

To connect a real Egyptian gateway, implement `PaymentProvider` in
`backend/billing/payments.py`, register it, and set `EGX_PAYMENT_PROVIDER` and
`EGX_PAYMENT_API_KEY`.

---

## Internal research terminal

The original research terminal — backtesting, paper trading, risk analytics,
thesis management, model evaluation — is mounted at **`/terminal`** and is
restricted to administrators. It is GMG's own tooling, not part of the
subscription. It runs in paper mode only; there is no broker integration and no
execution path anywhere in this repository.

---

## Testing

```bash
pytest                    # 485 tests
pytest --cov=backend      # with coverage
```

The suite is written around the failure modes that matter: a licensed provider
must refuse rather than silently downgrade to demo data; demo inputs must
contaminate every aggregate they touch; an expired-but-`ACTIVE` subscription
must be denied while a cancelled-but-paid one is honoured; premium markup must
be **absent** from the HTML rather than hidden in it; and an alert must never
fire on a generated price.

---

## Documentation

| File | Contents |
|---|---|
| [`PROJECT_PLAN.md`](PROJECT_PLAN.md) | Architecture and build plan |
| [`LEGAL.md`](LEGAL.md) | Regulatory position and the outstanding counsel review |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Production deployment and the go-live checklist |
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | Connecting a licensed market-data provider |
| [`database/SCHEMA.md`](database/SCHEMA.md) | Generated schema reference |

---

## Disclaimer

Investing in listed securities carries risk, including the loss of the capital
invested. Past performance does not indicate future results. Nothing produced
by this software is personal investment advice. Verify every figure against the
company's own published financial statements and the Egyptian Exchange's
official disclosures before acting on it.

© GMG AI Solutions.
