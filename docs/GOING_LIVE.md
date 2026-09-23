# Going live: attaching a real EGX market-data feed

The platform is complete. Scores, DCF, screens, alerts, portfolios and the
weekly report all work today — they have been reading generated demonstration
prices because no feed was attached. This document is how you attach one.

**Nothing in this document requires a code change.** Once you hold a vendor key,
going live is three settings and one verification command.

---

## 1. What you actually have to buy

This is the part that is not a programming problem, so it is worth being precise
about what you are purchasing. Three separate things get bundled under the word
"data", and they are priced separately:

| What | What it means | Why it matters here |
|---|---|---|
| **Feed access** | The vendor lets your server call their API. | Without it there is no data at all. |
| **Latency tier** | Real-time, delayed (usually 15 min), or end-of-day. | Sets `EGX_QUOTE_DELAY_MINUTES`. Real-time costs materially more. |
| **Redistribution** | Permission to *show* the data to people who are not you. | **This is the one that catches SaaS builders.** |

The third row is the important one. Showing EGX prices on a screen that a paying
subscriber looks at is *redistribution*, not internal use. Exchanges and vendors
license those differently, and a personal or "internal use only" plan does not
cover it — no matter how the API behaves technically.

**Before you sell a single subscription, ask your vendor this in writing:**

> I operate a subscription research website. My paying subscribers will see EGX
> prices on their screens. Does my licence permit display and redistribution to
> external end users, at what latency, and is there a per-user or per-display
> fee?

Get the answer in writing. It determines your data cost per subscriber, which
determines whether EGP 300/month works as a price.

### Where to look for EGX coverage

This is a shortlist to evaluate, not a recommendation, and coverage and pricing
change — confirm both directly with each vendor:

- **The Egyptian Exchange itself** (egx.com.eg) — the primary source, and the
  authority on who may redistribute EGX data.
- **Egyptian brokers and local data houses** — often the most practical route
  for EGX-specific coverage and local support.
- **International data APIs** with Egypt coverage — e.g. EODHD, Twelve Data.
  Two presets ship for these. Confirm EGX coverage on *your* plan before paying.
- **Institutional terminals** (LSEG/Refinitiv, Bloomberg, FactSet) — the
  fullest data, the highest cost, and redistribution terms to negotiate.

**Not an option:** scraping TradingView, an exchange website, or any vendor's
terminal. It breaches their terms, it breaks without warning, and it cannot be
licensed for redistribution. This codebase contains no scraper and will not
grow one.

---

## 1b. The cheapest real data: EGX's own bulletin

Before you buy anything, there is a source you already have access to — the
Egyptian Exchange publishes its own end-of-day trading data on egx.com.eg. It is
the **primary** source, which makes it the most defensible input this platform
can take, and its terms are negotiated with EGX rather than with a reseller.

Download the file and import it:

```bash
# always look first — this writes nothing
python scripts/import_egx_bulletin.py --file ~/Downloads/EGX_2026-09-22.xlsx --dry-run

# then, once the column mapping looks right
python scripts/import_egx_bulletin.py --file ~/Downloads/EGX_2026-09-22.xlsx
```

Set `EGX_BULLETIN_URL` to fetch it automatically, and a daily cron needs no
arguments at all.

**The format is data, not code.** EGX has changed its site and its file layout
more than once, publishes in Arabic and in English, and ships CSV in some places
and Excel in others. Nothing is read by column position. Headers are matched by
alias in both languages, the match is printed for you to check, and anything the
matcher gets wrong is corrected in a JSON file:

```bash
echo '{"close": "Closing Price", "ticker": "Reuters Code"}' > egx-map.json
python scripts/import_egx_bulletin.py --file b.xlsx --map egx-map.json
```

The dry run prints the mapping, a sample of parsed rows, and everything it
rejected with a reason:

```
  COLUMN MAPPING — check this before importing
    ticker           الكود
    close            سعر الإغلاق
    volume           حجم التداول
  · trades           — not present —

  REJECTED (1) — these are not stored
     1  no closing price: SUSP
```

What it refuses to do, and why each one matters:

- **A file whose required columns cannot be identified is refused whole**, with
  every header listed. Reading by position is how a layout change silently
  imports one company's price under another company's name.
- **A row with no usable positive close is rejected and counted.** A suspended
  stock shows `N/A`, never `0.00`.
- **An undated bulletin is never stamped with today's date.** The date comes
  from `--date`, a date column, or the filename — in that order. A file
  downloaded on Sunday covering Thursday would otherwise be stamped with a day
  on which nothing traded.
- **A code outside your covered universe is counted and skipped**, not turned
  into a company row from a spreadsheet cell.

### What this gives you, and what it does not

This is **end-of-day data**. It is stored with the bulletin's own trading date
and displayed with its true age — the platform shows "as of 22 Sep" rather than
implying a live tape. Scores, screens, valuations, the weekly report and
portfolio marks all run correctly on it.

It is **not real-time**, so intraday alerts and a moving dashboard still need a
licensed live feed (§2 below). And it does not settle the licensing question:
redistributing EGX data to paying subscribers is EGX's decision, whether the
data came from their website or from a vendor. Ask them.

---

## 2. Attach the feed

### If your vendor has a preset (EODHD, Twelve Data)

```bash
EGX_MARKET_DATA_VENDOR=eodhd          # or twelvedata
EGX_MARKET_DATA_API_KEY=your-key-here
EGX_QUOTE_DELAY_MINUTES=0             # 0 real-time, 15 delayed — what you PAY for
EGX_QUOTE_REFRESH_SECONDS=60          # within your plan's rate limit
```

### If your vendor has no preset — still no code

Copy `config/vendor-spec.example.json`, describe your vendor's JSON in it, and
point at it:

```bash
EGX_MARKET_DATA_SPEC_PATH=/etc/gmg/my-vendor.json
EGX_MARKET_DATA_API_KEY=your-key-here
```

A spec is a URL, an auth style, and a map from the vendor's JSON keys to quote
fields. Any vendor that returns JSON can be described this way. A spec file also
*overrides* a preset — so if a preset stops matching your account, you fix it in
a config file at 9am rather than waiting for a release.

---

## 3. Verify before trusting a single price

```bash
python scripts/verify_market_data.py --tickers COMI,HRHO,SWDY,ETEL,EAST
```

It calls the vendor exactly the way the platform will, prints the parsed quotes,
and exits `0` (all good), `1` (partial), or `2` (unusable) — so you can use it
as a deployment gate. It writes nothing to the database.

```
TICKER        PRICE       PREV    CHANGE        VOLUME  QUOTE TIME
--------------------------------------------------------------------------
COMI          62.25      63.38    -1.78%       659,053  2026-09-02 15:12Z  (0 min ago)
...
  5 of 5 tickers returned a usable quote.
  Labelled demo:    0  (must be 0 on a licensed feed)
  RESULT: feed usable. Start the app and quotes will be live.
```

### When a ticker comes back empty

Almost always the symbol, not the code. Vendors disagree about EGX symbology —
`COMI.EGX`, `COMI.CA`, `COMI:EGX`, `CIB.CA` are all real conventions.

```bash
python scripts/verify_market_data.py --tickers COMI --raw   # see what they sent
```

Then add the correct symbol to `config/symbol_map.json`:

```json
{ "eodhd": { "COMI": "COMI.CA", "HRHO": "HRHO.CA" } }
```

No restart of anything but the app. No code change.

---

## 4. What changes the moment the feed is attached

Nothing you have to do. The platform detects the feed at startup:

- The purple **DEMO DATA — NOT REAL-TIME** banner is replaced by a green
  **LIVE** banner naming the source.
- The scheduler registers a quote-refresh job. It does not exist on a demo
  instance, because polling generated prices on a timer would produce a tape
  that moves like a live one but is not.
- Alerts begin firing. They are suppressed on demo data by design — an alert
  about a price that does not exist is worse than no alert.
- Index composites, breadth, gainers and losers are computed from real quotes.
  They are still labelled *composites computed by GMG*, because they are not the
  official EGX index levels and never will be unless you license those too.
- Scores, screens and DCF valuations run on real inputs.

## 5. What does not change

The refusals stay in place, and they are the reason the numbers are worth
anything:

- A vendor row with no usable price is **dropped**, not zeroed. A suspended
  stock shows `N/A — data unavailable`.
- A row quoted in the wrong currency is **dropped**. A USD price under an EGP
  label is a wrong number, not a formatting problem.
- A vendor outage produces **nothing** — the last known quote keeps its true
  timestamp and age. It never falls back to demo prices.
- `EGX_QUOTE_DELAY_MINUTES` is shown to users verbatim. If you set `0` while
  holding a delayed licence, you are the one making the false claim; the
  verification script warns you when the timestamps disagree with the setting.

---

## 6. Still outstanding, and not solvable in code

**Egyptian legal review.** The legal documents in `frontend/templates/gmg/legal/`
are drafted but have not been reviewed by Egyptian counsel. Before taking a
paying customer, have a lawyer confirm whether any FRA, Central Bank,
consumer-protection, data-protection, tax or e-commerce authorisation applies to
this exact model — selling research subscriptions without holding client funds,
holding securities, or executing trades — and approve the wording. See
`LEGAL.md` §5. The platform makes no claim of FRA licensing and must not until
this is settled.

**A payment gateway.** `backend/billing/` defines a provider interface with no
gateway bound and no card handling anywhere. Connect an Egyptian payment
provider through that interface when you have a merchant account.
