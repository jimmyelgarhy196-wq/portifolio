# GMG Investment Intelligence — Final Build Report

**GMG AI Solutions** · build completed 31 August 2026 · 512 automated tests passing

---

## 1. What was built

An existing EGX research codebase was **inspected and extended** — not restarted —
into a subscription research platform.

**Kept and built upon:** the data-quality primitives (`UNAVAILABLE`, `safe_div`,
`safe_growth`), the provenance model on every stored row, the scoring framework with
weight redistribution, the point-in-time data access that prevents look-ahead, the
fundamental/technical/quantitative engines, the research agents, the backtesting
engine and the weekly report.

**Added on top:**

| Layer | Modules |
|---|---|
| Market data | `backend/market/`: EGX session state, quote providers with provenance, index/mover/breadth aggregation, search |
| Accounts | `backend/accounts/`: Argon2id hashing, one-time tokens, CSRF, registration, sessions, lockout |
| Billing | `backend/billing/`: subscription lifecycle and entitlement, payment-provider abstraction |
| Notifications | `backend/notify/`: email-provider abstraction, user alert engine |
| Analysis | `backend/analytics/valuation.py`, `screener.py`; `backend/research/rating.py` |
| Web | `backend/api/`: auth dependencies + six route modules |
| Interface | `gmg.css` design system, four JS modules, 58 templates including 7 legal documents and an admin panel |

**Customer-facing surface:** landing, pricing, about, contact, data-source
transparency, 7 legal pages, sign-up/sign-in/verification/reset, market dashboard,
stock list, sector view, a 10-tab stock page, screener with saved screens,
watchlists, portfolio tracking, alerts, research rankings, weekly reports,
valuation tools, account and subscription management, and an admin panel.

## 2. What is real and what is demonstration data

| Component | Status |
|---|---|
| Market prices | **No licensed feed is connected.** The platform serves stored end-of-day bars if present, otherwise generated demonstration data, labelled `DEMO DATA — NOT REAL-TIME` everywhere it appears |
| Index levels | **Never synthesised.** Shown as official only when a feed supplies them; otherwise a constituent composite explicitly labelled as computed by GMG |
| Financial statements | Whatever is stored. The demo seed writes fictional statements stamped `SYNTHETIC_DEMO` |
| Scores, ratios, valuations | **Real computations** over whatever data is stored — the arithmetic is genuine even when the inputs are demonstration data |
| News and disclosures | Stored records with their source. The demo seed writes items prefixed `[DEMO]` |
| Payments | **No gateway connected.** Checkout records intent and states that no card was charged |
| Email | Console provider by default; SMTP works when configured |
| AI commentary | Deterministic narrative engine unless `ANTHROPIC_API_KEY` is set. Ratings and scores never depend on the model |

Every quote carries `source`, `quote_time`, `retrieved_at`, `delayed_minutes` and
`is_demo` in the database, and all five are visible in the UI.

## 3. Functional vs placeholder

**Fully functional:** accounts and sessions, email verification, password reset,
subscription lifecycle and entitlement, server-side access control, the admin panel,
watchlists, portfolio tracking, alerts (evaluation and email), the screener, all
scoring engines, the DCF and multiples valuation with sensitivity, rating derivation,
charting, search, the weekly report, and the audit log.

**Deliberate connection points, not placeholders** — each has the correct interface,
refuses rather than faking, and says what is missing:

| Point | Where | Behaviour without it |
|---|---|---|
| Licensed quote feed | `LicensedQuoteProvider.get_quotes()` | Raises; falls back to stored, then labelled demo |
| Payment gateway | `PaymentProvider` subclass | Manual mode: records intent, charges nothing |
| Official index levels | `IndexLevelProvider` | Composite shown, labelled as not official |

**Not built:** no broker integration, no order execution, no custody — by design and
by the brief.

## 4. Third-party services required for production

| Service | Purpose | Without it |
|---|---|---|
| Licensed EGX market-data vendor | Real-time or delayed quotes | Demo data, clearly labelled |
| Egyptian payment gateway | Collecting subscriptions | Manual confirmation by an administrator |
| SMTP or transactional-email API | Verification, resets, alerts, reports | Emails logged, not delivered |
| PostgreSQL | Production database | SQLite works but is not recommended |
| Anthropic API *(optional)* | AI narrative | Deterministic narrative instead |

## 5. Environment variables

All **78** settings are documented in [`.env.example`](.env.example), each explaining
what its absence causes. The cross-check that every setting is documented and no
documented key is stale is part of the build.

**Required in production:** `EGX_AUTH_SECRET` (the app refuses to start without it),
`EGX_ENV=production`, `EGX_COOKIE_SECURE=true`, `EGX_DATABASE_URL`,
`EGX_BASE_URL`, `EGX_ALLOW_SYNTHETIC_DATA=false`.

## 6. Database

**36 tables** — 20 research tables (companies, prices, statements, valuations, news,
disclosures, theses, recommendations, scores, portfolios, positions, trades,
snapshots, watchlist, alerts, reports, backtests, settings, data-quality log) and 16
platform tables (users, sessions, one-time tokens, login attempts, subscriptions,
payments, user watchlists and items, user portfolios and positions, saved screens,
user alerts, audit log, email log, quote cache, data sources).

One `init_database()` creates all of them. Generated reference:
[`database/SCHEMA.md`](database/SCHEMA.md).

## 7. Security

- **Argon2id** password hashing (64 MiB, t=3, p=4); plaintext never logged, stored or
  put in an audit row; hashes upgraded automatically when cost parameters rise
- Session tokens stored only as **SHA-256 digests**; cookies **HMAC-signed**,
  `HttpOnly`, `SameSite=Lax`, `Secure` in production
- A **session epoch** on each user: changing a password invalidates every session
- **CSRF on every state-changing form**, including sign-in and sign-up, where an
  anonymous visitor gets a short-lived HttpOnly form cookie — closing login CSRF
- **Rate limiting and lockout** per IP and per account on sign-in, sign-up, reset,
  verification resend and contact
- **No user enumeration**: sign-up, sign-in and reset return identical responses
  whether or not the address exists, with comparable timing
- **Entitlement enforced server-side before rendering.** Premium markup is never sent
  to the browser; premium JSON returns 402. Editing frontend JavaScript unlocks nothing
- **Per-user scoping** on every user-owned query; another user's id returns 404
- **Open redirects closed** — `?next=` accepts same-site paths only
- No hardcoded credentials, no interpolated SQL, no disabled template escaping, no
  debug flags (verified by sweep)
- Card details never reach the server; there is no card handling code at all

## 8. Legal and compliance

Seven documents published at `/terms`, `/privacy`, `/disclaimer`,
`/risk-disclosure`, `/subscription-terms`, `/refund-policy`, `/cookies`, each written
to describe GMG's actual model.

Stated in the negative, repeatedly and prominently: GMG **does not** accept or hold
client money, hold securities, execute trades, manage portfolios, give personal
advice, or guarantee any return — and is **not licensed or registered by the FRA** or
any other regulator. Prohibited language ("guaranteed return", "risk-free") is absent;
[`LEGAL.md`](LEGAL.md) §6 carries the audit command.

> ### ⚠️ OUTSTANDING — BLOCKS COMMERCIAL LAUNCH
> **Have Egyptian legal counsel review all legal documents and confirm whether any
> FRA, Central Bank, consumer-protection, data-protection, tax, e-commerce or other
> authorization/registration requirements apply to the exact GMG business model.**
>
> The documents are drafts. They have **not** been reviewed by a qualified Egyptian
> lawyer. Nine specific questions for counsel are listed in [`LEGAL.md`](LEGAL.md) §5,
> and the requirement is recorded in code at
> `backend/api/routes_public.py::LEGAL_REVIEW_TODO`.

## 9. What is missing, and what to do next

| Item | Why it is not done | Next step |
|---|---|---|
| Real market data | No licence has been signed; the sandbox also blocks every financial-data host | Sign a vendor licence, implement `LicensedQuoteProvider.get_quotes()` |
| Payment collection | No Egyptian gateway credentials | Implement `PaymentProvider`, set the keys, point the webhook at `/api/payments/webhook` |
| Legal sign-off | Requires a qualified Egyptian lawyer | See `LEGAL.md` §5 |
| Official index levels | Requires an exchange feed | Implement `IndexLevelProvider` |
| Arabic interface | Out of scope for this build | The data model already carries `name_ar` |
| Redis-backed rate limiting | Single-node deployment works as-is | Swap the store in `auth_deps.rate_limit`; call sites unchanged |
| Real EGX universe | The seeded list is reference-only and marked unverified | `scripts/refresh_universe.py` against the official constituent lists |

## 10. Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print('EGX_AUTH_SECRET=' + secrets.token_urlsafe(48))" >> .env
python scripts/init_db.py
python scripts/refresh_universe.py
python scripts/run_server.py            # http://127.0.0.1:8000
```

To explore with demonstration data (every figure fictional and labelled):

```bash
EGX_ALLOW_SYNTHETIC_DATA=true python scripts/seed_gmg_demo.py --reset
python scripts/research.py
```

The first account registered becomes the administrator.

## 11. Deploying it

[`DEPLOYMENT.md`](DEPLOYMENT.md) covers production settings, systemd and nginx
configuration, the two in-process components that must move to shared storage before
a second node, connecting a data feed and a payment gateway, backups, and a go-live
checklist across security, data, legal, billing and operations.

## 12. Testing performed

**512 automated tests**, 171 written for this platform:

- *Market* (31): demo determinism and stamping, the licensed provider refusing rather
  than downgrading, all five freshness badges, index levels never invented, demo
  contamination reaching every aggregate, breadth reconciliation, search ranking
- *Access* (70): four privilege levels across every gated route; premium markup
  **absent** from the HTML rather than hidden; 402 on premium JSON; expired-but-ACTIVE
  denied and cancelled-but-paid honoured; forged and unsigned cookies rejected; CSRF
  including anonymous forms; one user unable to touch another's data; duplicate-name
  handling
- *Analysis* (46): DCF refusals and growth fade, CAPM, sensitivity monotonicity,
  dispersion withholding, rating bands and withholding, screener exclusion on unknown
  values, alerts never firing on demonstration data
- *Report rendering* (24): headings, tables with numeric alignment, merged
  blockquotes, and — the reason this renderer exists rather than a dependency —
  that HTML, `<script>`, event handlers and `javascript:` targets in the source
  text are all escaped before any formatting is applied
- *Pre-existing* (341): data quality, scoring, engines, research, portfolio, risk,
  backtesting, reports, API

**Browser QA** with Chromium: a 22-step user journey — land, price, sign up, sign in,
search, chart, premium tab, subscription, valuation, watchlist, portfolio, alert,
screener, legal — passing **44/44 on desktop (1440px) and mobile (390px)**; plus a
sweep of **90 page loads** over every public, application, stock-tab, admin and
terminal route at both widths with no non-200 responses, no horizontal overflow, no
unrendered template artefacts and no JavaScript errors.

**Real bugs the QA found and fixed:** the screener's fundamental columns were always
N/A because metrics were never passed; blended valuation averaged methods that
disagreed six-fold into one confident number; a duplicate watchlist name returned 500;
`p.values` in a template resolved to `dict.values()`; two stylesheets collided on 32
class names; `/favicon.ico` 404'd on every page.

## 13. Known limitations

1. **The demonstration data is fictional.** It uses real ticker symbols and company
   names so the product is recognisable, but no figure attached to them is real. It is
   labelled throughout and must be deleted before any customer sees the platform.
2. **The seeded EGX universe is reference-only** and is flagged unverified in the UI
   until reconciled against the exchange's official constituent lists.
3. **Index composites are not index levels.** They describe the average move of the
   constituents GMG holds quotes for, and are labelled as such.
4. **A DCF is only as good as its assumptions.** The model publishes its sensitivity
   grid for that reason, and refuses to produce a figure where the arithmetic would be
   meaningless.
5. **Peer multiples need a populated sector.** Fewer than three peers yields no peer
   context rather than a bad one.
6. **Rate limiting is in-process.** Correct on one node; move it to Redis for more.
7. **The scheduler must run on exactly one node.**
8. **Arabic is stored but not rendered.** The UI is English.
9. **AI commentary can still be wrong** even constrained to available data. It is
   labelled, separated from the figures, and never produces a number that enters a score.
10. **No legal sign-off yet.** See §8.

---

*GMG AI Solutions provides research and information only. It does not accept client
money, hold securities, execute trades or manage portfolios, and guarantees no return.
Investing on the Egyptian Exchange carries the risk of losing capital.*
