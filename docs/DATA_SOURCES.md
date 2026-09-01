# Getting real data into GMG Investment Intelligence

The system ships with **no market data**. That is deliberate: an empty terminal is
honest, and a pre-filled one would be lying about what it knows. This document
covers the three ways to fill it.

---

## 1. CSV import — recommended, no credentials

The most reliable route, and the only one that covers EGX **fundamentals**
properly. You export data from a source you trust and drop it into
`data/manual/`. The provider records the filename as the source, so provenance
points at real bytes you control.

```bash
mkdir -p data/manual/{prices,fundamentals}
# ... add your files ...
python scripts/ingest.py --provider csv --dataset all
```

### `data/manual/prices/<TICKER>.csv`

```csv
date,open,high,low,close,adjusted_close,volume
2024-01-02,80.00,81.20,79.50,81.00,80.90,120000
2024-01-03,81.00,82.10,80.75,81.85,81.75,98500
```

| Column | Required | Notes |
|---|---|---|
| `date` | yes | `YYYY-MM-DD`, `DD/MM/YYYY`, `YYYYMMDD` all accepted |
| `close` | yes | A row without a close is skipped, never interpolated |
| `open`, `high`, `low` | no | Improve ATR, Bollinger and candle-based signals |
| `adjusted_close` | no | Defaults to `close`. Supply it if you have it — splits and dividends distort every return calculation otherwise |
| `volume` | no | Required for volume confirmation, liquidity risk and turnover |

Accepted number formats: `1,234.56`, `(1234)` for negatives, `1.5M`, `2K`, `15%`.
Unparseable values become `NULL`, never `0`.

### `data/manual/fundamentals/<TICKER>.csv`

```csv
period,period_type,period_end,available_from,revenue,gross_profit,ebitda,operating_income,net_income,eps,cash,total_debt,total_assets,total_equity,operating_cash_flow,capex,interest_expense,current_assets,current_liabilities,dividends_paid
2024-FY,FY,2024-12-31,2025-03-15,12000000000,4800000000,2900000000,2400000000,1800000000,3.50,1500000000,3000000000,14000000000,9000000000,2100000000,600000000,220000000,5200000000,2600000000,540000000
```

**`available_from` is the single most important column in this file.** It is the
date the statement was actually *published*, not the date the period ended. The
backtester filters on it, so a December year-end that reported in March stays
invisible until March. Omit it and the system assumes a conservative 90-day
statutory lag — safe, but less accurate than the real filing date.

`free_cash_flow` is computed as `operating_cash_flow − |capex|` when absent.

### `data/manual/companies.csv` (optional)

```csv
ticker,name,sector,industry,currency,shares_outstanding
COMI,Commercial International Bank,Banks,Banking,EGP,2950000000
```

`shares_outstanding` unlocks market cap and every valuation multiple. Without it
P/B, P/S, EV/EBITDA and FCF yield stay `UNAVAILABLE`.

### `data/manual/news.csv` and `data/manual/disclosures.csv`

```csv
ticker,title,source,url,publication_date,summary
COMI,Board approves cash dividend,EGX,https://...,2025-03-14,Dividend of EGP 5.00 per share
```

```csv
ticker,title,date,type,url,summary
COMI,FY2024 financial statements,2025-03-15,EARNINGS,https://...,
```

---

## 2. Yahoo Finance — prices only, no credentials

EGX equities carry a `.CA` suffix; the EGX30 index is `^CCSI`.

```bash
python scripts/ingest.py --provider yahoo --dataset prices --index egx30
```

**Coverage caveat, stated plainly:** Yahoo carries EGX prices reliably but its
EGX fundamentals coverage is sparse to absent. That is why `EGX_FUNDAMENTAL_PROVIDERS`
defaults to `csv`. Do not expect this route to populate financial statements.

Check connectivity before a full run:

```bash
python scripts/ingest.py --dry-run
```

---

## 3. A commercial provider

Implement the interface in `backend/data/providers/base.py`:

```python
from backend.data.providers.base import MarketDataProvider, PriceBarDTO, ProviderCapabilities

class MyProvider(MarketDataProvider):
    name = "myprovider"

    def capabilities(self):
        return ProviderCapabilities(name=self.name, domains={"prices"},
                                    requires_credentials=True)

    def is_available(self):
        return bool(get_settings().aggregator_api_key)

    def get_price_history(self, ticker, start, end, *, symbol_hint=None):
        # Return [] rather than raising when there is simply no coverage.
        # Raise ProviderUnavailable when the service itself failed.
        return [PriceBarDTO(ticker=ticker, timestamp=..., close=...,
                            quality=self.quality())]
```

Register it in `backend/data/providers/registry.py`, then add its name to the
relevant `EGX_*_PROVIDERS` variable. Chains are tried in order and the first
success wins, so `EGX_MARKET_DATA_PROVIDERS=myprovider,csv,yahoo` gives you a
primary with two fallbacks.

Credential slots for Refinitiv, Bloomberg and a generic aggregator already exist
in `.env.example`.

---

## The contract every provider must honour

1. **Return data, or return nothing.** Never substitute a placeholder for a
   missing value. `None` is a valid answer; `0.0` is a lie.
2. **Stamp provenance.** Every record carries a real source name and retrieval
   time via `self.quality()`.
3. **Be idempotent.** Fetching the same range twice yields the same records, so
   ingestion can safely de-duplicate.
4. **Distinguish "no coverage" from "failure."** Return `[]` for the former;
   raise `ProviderUnavailable` for the latter. The ingestion layer records
   failures in `data_quality_log` so the UI can explain *why* data is missing.

---

## Verifying the universe

The shipped `config/universe.yaml` is a reference seed compiled from general
knowledge and flagged `verified: false`. Index membership changes as EGX reviews
its constituents, and the UI shows a warning banner until you reconcile it:

```bash
# Download the current constituent list from https://www.egx.com.eg
python scripts/refresh_universe.py --from-csv egx30_constituents.csv --mark-verified
```

---

## Checking what actually loaded

```bash
python scripts/ingest.py --dry-run     # provider availability
```

Then in the terminal, **Settings → Recent Data Ingestion** shows every run: which
provider served it, how many rows landed, how many were skipped, and the reason
for any failure. That audit trail is the answer to "why is this figure missing?"
