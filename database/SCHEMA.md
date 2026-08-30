12:00:54 INFO    backend.core.database        Database initialised at sqlite:////home/user/portifolio/database/egx_alpha.db
# EGX ALPHA — Database Schema

Auto-generated from the SQLAlchemy models. Regenerate after a model change with:

```bash
python scripts/gen_schema.py > database/SCHEMA.md
```

## Provenance convention

Tables holding ingested or derived data carry four data-quality columns —
`source`, `retrieved_at`, `data_period`, `confidence` — via `ProvenanceMixin`.
This is what makes "every numerical claim comes from a source" enforceable at
the storage layer rather than by convention. Those tables are marked
**(provenance)** below.

## Append-only tables

`recommendations`, `score_history`, `thesis_versions` and `reports` are never
updated in place. They are the system's memory, and grading its own past
predictions depends on that history being immutable.

---

## `alerts`

Generated alerts. Notifications are dispatched only when a channel is explicitly configured.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | yes |  |
| `alert_type` | VARCHAR(48) | no |  |
| `severity` | VARCHAR(16) | no |  |
| `title` | VARCHAR(255) | no |  |
| `message` | TEXT | yes |  |
| `payload` | JSON | no |  |
| `status` | VARCHAR(16) | no |  |
| `created_at` | DATETIME | no |  |
| `notified` | BOOLEAN | no |  |

**Indexes:** `ix_alert_created` (created_at, status), `ix_alerts_alert_type` (alert_type), `ix_alerts_created_at` (created_at), `ix_alerts_ticker` (ticker)

---

## `backtest_runs`

Backtest configurations, metrics, equity curves and trades.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `name` | VARCHAR(128) | no |  |
| `strategy` | VARCHAR(64) | no |  |
| `start_date` | DATE | no |  |
| `end_date` | DATE | no |  |
| `initial_capital` | FLOAT | no |  |
| `parameters` | JSON | no |  |
| `metrics` | JSON | no |  |
| `equity_curve` | JSON | no |  |
| `trades` | JSON | no |  |
| `created_at` | DATETIME | no |  |
| `contains_synthetic_data` | BOOLEAN | no |  |

**Indexes:** `ix_backtest_runs_created_at` (created_at), `ix_backtest_runs_strategy` (strategy)

---

## `companies`

The EGX universe: ticker, name, sector, index membership and provider symbol mapping. Reference data only — no prices or financials.

| Column | Type | Null | Default |
|---|---|---|---|
| `company_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `name` | VARCHAR(255) | no |  |
| `name_ar` | VARCHAR(255) | yes |  |
| `sector` | VARCHAR(96) | yes |  |
| `industry` | VARCHAR(128) | yes |  |
| `exchange` | VARCHAR(16) | no |  |
| `currency` | VARCHAR(8) | no |  |
| `listing_date` | DATE | yes |  |
| `status` | VARCHAR(24) | no |  |
| `in_egx30` | BOOLEAN | no |  |
| `in_egx70` | BOOLEAN | no |  |
| `in_egx100` | BOOLEAN | no |  |
| `provider_symbols` | JSON | no |  |
| `shares_outstanding` | FLOAT | yes |  |
| `description` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Indexes:** `ix_companies_sector` (sector), `ix_companies_ticker` (ticker)

---

## `data_quality_log`

Audit trail of every ingestion run: provider, status, rows in, rows skipped, and the reason for any failure.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `dataset` | VARCHAR(48) | no |  |
| `ticker` | VARCHAR(24) | yes |  |
| `provider` | VARCHAR(48) | yes |  |
| `status` | VARCHAR(24) | no |  |
| `rows_ingested` | INTEGER | no |  |
| `rows_skipped` | INTEGER | no |  |
| `message` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |

**Indexes:** `ix_data_quality_log_created_at` (created_at), `ix_data_quality_log_ticker` (ticker), `ix_dq_dataset_time` (dataset, created_at)

---

## `disclosures`  **(provenance)**

Official corporate disclosures, classified by type and importance. These drive the catalyst score.

| Column | Type | Null | Default |
|---|---|---|---|
| `disclosure_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | yes |  |
| `title` | TEXT | no |  |
| `date` | DATE | yes |  |
| `disclosure_type` | VARCHAR(64) | yes |  |
| `url` | TEXT | yes |  |
| `url_hash` | VARCHAR(64) | no |  |
| `summary` | TEXT | yes |  |
| `importance` | INTEGER | yes |  |
| `source` | VARCHAR(64) | no |  |
| `retrieved_at` | DATETIME | no |  |
| `data_period` | VARCHAR(32) | yes |  |
| `confidence` | VARCHAR(16) | no |  |

**Unique constraints:** `uq_disclosure_url_hash` (url_hash)

**Indexes:** `ix_disclosure_ticker_date` (ticker, date), `ix_disclosures_date` (date), `ix_disclosures_ticker` (ticker)

---

## `financial_statements`  **(provenance)**

Reported financials. `available_from` records when a statement was actually published — the backtester filters on it, not on `period_end`, which is what prevents look-ahead bias.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `period` | VARCHAR(24) | no |  |
| `period_type` | VARCHAR(12) | no |  |
| `period_end` | DATE | no |  |
| `available_from` | DATE | yes |  |
| `revenue` | FLOAT | yes |  |
| `gross_profit` | FLOAT | yes |  |
| `ebitda` | FLOAT | yes |  |
| `operating_income` | FLOAT | yes |  |
| `net_income` | FLOAT | yes |  |
| `eps` | FLOAT | yes |  |
| `cash` | FLOAT | yes |  |
| `total_debt` | FLOAT | yes |  |
| `total_assets` | FLOAT | yes |  |
| `total_equity` | FLOAT | yes |  |
| `operating_cash_flow` | FLOAT | yes |  |
| `capex` | FLOAT | yes |  |
| `free_cash_flow` | FLOAT | yes |  |
| `interest_expense` | FLOAT | yes |  |
| `current_assets` | FLOAT | yes |  |
| `current_liabilities` | FLOAT | yes |  |
| `dividends_paid` | FLOAT | yes |  |
| `source` | VARCHAR(64) | no |  |
| `retrieved_at` | DATETIME | no |  |
| `data_period` | VARCHAR(32) | yes |  |
| `confidence` | VARCHAR(16) | no |  |

**Unique constraints:** `uq_fin_ticker_period` (ticker, period, period_type)

**Indexes:** `ix_fin_ticker_period` (ticker, period_end), `ix_financial_statements_available_from` (available_from), `ix_financial_statements_ticker` (ticker)

---

## `news`  **(provenance)**

News items with lexicon sentiment. `sentiment` is NULL when no lexicon term matched — 'no signal' and 'neutral' are different claims.

| Column | Type | Null | Default |
|---|---|---|---|
| `news_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | yes |  |
| `title` | TEXT | no |  |
| `news_source` | VARCHAR(128) | yes |  |
| `url` | TEXT | yes |  |
| `url_hash` | VARCHAR(64) | no |  |
| `publication_date` | DATETIME | yes |  |
| `summary` | TEXT | yes |  |
| `sentiment` | FLOAT | yes |  |
| `sentiment_label` | VARCHAR(16) | yes |  |
| `importance` | INTEGER | yes |  |
| `source` | VARCHAR(64) | no |  |
| `retrieved_at` | DATETIME | no |  |
| `data_period` | VARCHAR(32) | yes |  |
| `confidence` | VARCHAR(16) | no |  |

**Unique constraints:** `uq_news_url_hash` (url_hash)

**Indexes:** `ix_news_publication_date` (publication_date), `ix_news_ticker` (ticker), `ix_news_ticker_date` (ticker, publication_date)

---

## `portfolio_snapshots`

Daily valuations. Volatility, beta, drawdown and VaR all depend on this history existing.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `portfolio_id` | INTEGER | no |  |
| `as_of` | DATE | no |  |
| `total_value` | FLOAT | no |  |
| `cash` | FLOAT | no |  |
| `invested_value` | FLOAT | no |  |
| `unrealized_pnl` | FLOAT | no |  |
| `realized_pnl` | FLOAT | no |  |
| `benchmark_value` | FLOAT | yes |  |

**Unique constraints:** `uq_snapshot_portfolio_date` (portfolio_id, as_of)

**Indexes:** `ix_portfolio_snapshots_as_of` (as_of), `ix_portfolio_snapshots_portfolio_id` (portfolio_id)

**Foreign keys:** portfolio_id → portfolios.portfolio_id

---

## `portfolios`

Paper portfolios. `mode` is always PAPER; the column exists so a future live mode would be explicit rather than implicit.

| Column | Type | Null | Default |
|---|---|---|---|
| `portfolio_id` | INTEGER | no |  |
| `name` | VARCHAR(96) | no |  |
| `mode` | VARCHAR(16) | no |  |
| `currency` | VARCHAR(8) | no |  |
| `initial_capital` | FLOAT | no |  |
| `cash` | FLOAT | no |  |
| `benchmark_ticker` | VARCHAR(24) | no |  |
| `created_at` | DATETIME | no |  |
| `settings` | JSON | no |  |

**Unique constraints:** `None` (name)

---

## `positions`

Open positions, long and paper-short.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `portfolio_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `direction` | VARCHAR(12) | no |  |
| `quantity` | FLOAT | no |  |
| `average_price` | FLOAT | no |  |
| `current_price` | FLOAT | yes |  |
| `market_value` | FLOAT | yes |  |
| `portfolio_weight` | FLOAT | yes |  |
| `unrealized_pnl` | FLOAT | yes |  |
| `realized_pnl` | FLOAT | no |  |
| `strategy` | VARCHAR(48) | yes |  |
| `sector` | VARCHAR(96) | yes |  |
| `thesis_id` | INTEGER | yes |  |
| `opened_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `uq_position` (portfolio_id, ticker, direction)

**Indexes:** `ix_positions_portfolio_id` (portfolio_id), `ix_positions_strategy` (strategy), `ix_positions_ticker` (ticker)

**Foreign keys:** portfolio_id → portfolios.portfolio_id, thesis_id → research_theses.thesis_id

---

## `price_history`  **(provenance)**

Daily OHLCV bars. A unique constraint on (ticker, timestamp) is the primary defence against double ingestion. Market holidays are simply absent rows; no bar is ever synthesised.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `company_id` | INTEGER | yes |  |
| `ticker` | VARCHAR(24) | no |  |
| `timestamp` | DATE | no |  |
| `open` | FLOAT | yes |  |
| `high` | FLOAT | yes |  |
| `low` | FLOAT | yes |  |
| `close` | FLOAT | yes |  |
| `adjusted_close` | FLOAT | yes |  |
| `volume` | FLOAT | yes |  |
| `source` | VARCHAR(64) | no |  |
| `retrieved_at` | DATETIME | no |  |
| `data_period` | VARCHAR(32) | yes |  |
| `confidence` | VARCHAR(16) | no |  |

**Unique constraints:** `uq_price_ticker_ts` (ticker, timestamp)

**Indexes:** `ix_price_history_company_id` (company_id), `ix_price_history_ticker` (ticker), `ix_price_ticker_ts` (ticker, timestamp)

**Foreign keys:** company_id → companies.company_id

---

## `recommendations`

Every recommendation ever made. APPEND-ONLY, so model evaluation measures what the system said at the time rather than a reconstruction.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `thesis_id` | INTEGER | yes |  |
| `created_at` | DATETIME | no |  |
| `action` | VARCHAR(12) | no |  |
| `direction` | VARCHAR(12) | no |  |
| `strategy` | VARCHAR(48) | yes |  |
| `sector` | VARCHAR(96) | yes |  |
| `price_at_reco` | FLOAT | yes |  |
| `target_price` | FLOAT | yes |  |
| `invalidation_price` | FLOAT | yes |  |
| `conviction` | FLOAT | yes |  |
| `alpha_score` | FLOAT | yes |  |
| `expected_return` | FLOAT | yes |  |
| `expected_holding_period` | VARCHAR(48) | yes |  |
| `rationale` | TEXT | yes |  |
| `outcome_status` | VARCHAR(24) | no |  |
| `outcome_price` | FLOAT | yes |  |
| `outcome_date` | DATETIME | yes |  |
| `realized_return` | FLOAT | yes |  |
| `benchmark_return` | FLOAT | yes |  |
| `holding_days` | INTEGER | yes |  |

**Indexes:** `ix_reco_ticker_date` (ticker, created_at), `ix_recommendations_created_at` (created_at), `ix_recommendations_sector` (sector), `ix_recommendations_strategy` (strategy), `ix_recommendations_ticker` (ticker)

**Foreign keys:** thesis_id → research_theses.thesis_id

---

## `reports`

Stored weekly investment committee reports, as markdown and as structured sections so they can be compared programmatically.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `report_type` | VARCHAR(48) | no |  |
| `title` | VARCHAR(255) | no |  |
| `period_start` | DATE | yes |  |
| `period_end` | DATE | yes |  |
| `created_at` | DATETIME | no |  |
| `markdown` | TEXT | yes |  |
| `sections` | JSON | no |  |
| `generated_by` | VARCHAR(32) | no |  |
| `contains_synthetic_data` | BOOLEAN | no |  |

**Indexes:** `ix_report_period` (report_type, period_end), `ix_reports_created_at` (created_at), `ix_reports_period_end` (period_end)

---

## `research_theses`

Durable investment theses. One per (ticker, direction), updated weekly rather than replaced.

| Column | Type | Null | Default |
|---|---|---|---|
| `thesis_id` | INTEGER | no |  |
| `reference` | VARCHAR(24) | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `direction` | VARCHAR(12) | no |  |
| `strategy` | VARCHAR(48) | no |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |
| `entry_price` | FLOAT | yes |  |
| `target_price` | FLOAT | yes |  |
| `invalidation_price` | FLOAT | yes |  |
| `expected_return` | FLOAT | yes |  |
| `expected_downside` | FLOAT | yes |  |
| `risk_reward` | FLOAT | yes |  |
| `expected_holding_period` | VARCHAR(48) | yes |  |
| `conviction` | FLOAT | yes |  |
| `fundamental_score` | FLOAT | yes |  |
| `technical_score` | FLOAT | yes |  |
| `quant_score` | FLOAT | yes |  |
| `catalyst_score` | FLOAT | yes |  |
| `risk_score` | FLOAT | yes |  |
| `alpha_score` | FLOAT | yes |  |
| `thesis_text` | TEXT | yes |  |
| `bull_case` | TEXT | yes |  |
| `bear_case` | TEXT | yes |  |
| `catalysts` | JSON | no |  |
| `risks` | JSON | no |  |
| `invalidation_conditions` | JSON | no |  |
| `data_sources` | JSON | no |  |
| `statements` | JSON | no |  |
| `status` | VARCHAR(24) | no |  |
| `generated_by` | VARCHAR(32) | no |  |
| `version` | INTEGER | no |  |

**Unique constraints:** `None` (reference)

**Indexes:** `ix_research_theses_ticker` (ticker), `ix_thesis_ticker_status` (ticker, status)

---

## `score_history`

Score per ticker per date. APPEND-ONLY, powering week-over-week deltas and score-change alerts.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `as_of` | DATE | no |  |
| `alpha_score` | FLOAT | yes |  |
| `fundamental_score` | FLOAT | yes |  |
| `technical_score` | FLOAT | yes |  |
| `quant_score` | FLOAT | yes |  |
| `catalyst_score` | FLOAT | yes |  |
| `quality_score` | FLOAT | yes |  |
| `risk_score` | FLOAT | yes |  |
| `sentiment_score` | FLOAT | yes |  |
| `confidence` | VARCHAR(16) | yes |  |
| `breakdown` | JSON | no |  |

**Unique constraints:** `uq_score_ticker_date` (ticker, as_of)

**Indexes:** `ix_score_history_ticker` (ticker), `ix_score_ticker_date` (ticker, as_of)

---

## `settings`

User settings overriding YAML and environment defaults.

| Column | Type | Null | Default |
|---|---|---|---|
| `key` | VARCHAR(96) | no |  |
| `value` | JSON | no |  |
| `updated_at` | DATETIME | no |  |

---

## `thesis_versions`

Immutable snapshots of prior thesis states. APPEND-ONLY — this is what makes 'what changed since last week?' answerable from the record.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `thesis_id` | INTEGER | no |  |
| `version` | INTEGER | no |  |
| `created_at` | DATETIME | no |  |
| `snapshot` | JSON | no |  |
| `change_summary` | TEXT | yes |  |

**Unique constraints:** `uq_thesis_version` (thesis_id, version)

**Indexes:** `ix_thesis_versions_thesis_id` (thesis_id)

**Foreign keys:** thesis_id → research_theses.thesis_id

---

## `trades`

Immutable paper-trade ledger with commission and slippage on every fill.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `portfolio_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `side` | VARCHAR(12) | no |  |
| `direction` | VARCHAR(12) | no |  |
| `quantity` | FLOAT | no |  |
| `price` | FLOAT | no |  |
| `commission` | FLOAT | no |  |
| `slippage` | FLOAT | no |  |
| `gross_value` | FLOAT | no |  |
| `net_value` | FLOAT | no |  |
| `realized_pnl` | FLOAT | yes |  |
| `executed_at` | DATETIME | no |  |
| `strategy` | VARCHAR(48) | yes |  |
| `thesis_id` | INTEGER | yes |  |
| `note` | TEXT | yes |  |
| `mode` | VARCHAR(16) | no |  |

**Indexes:** `ix_trade_portfolio_date` (portfolio_id, executed_at), `ix_trades_executed_at` (executed_at), `ix_trades_portfolio_id` (portfolio_id), `ix_trades_ticker` (ticker)

**Foreign keys:** portfolio_id → portfolios.portfolio_id

---

## `valuation_snapshots`  **(provenance)**

Multiples derived from price + fundamentals + share count. Every field is a CALCULATION; any multiple whose inputs are missing stays NULL.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `date` | DATE | no |  |
| `market_cap` | FLOAT | yes |  |
| `enterprise_value` | FLOAT | yes |  |
| `pe` | FLOAT | yes |  |
| `pb` | FLOAT | yes |  |
| `ps` | FLOAT | yes |  |
| `ev_ebitda` | FLOAT | yes |  |
| `ev_sales` | FLOAT | yes |  |
| `fcf_yield` | FLOAT | yes |  |
| `dividend_yield` | FLOAT | yes |  |
| `source` | VARCHAR(64) | no |  |
| `retrieved_at` | DATETIME | no |  |
| `data_period` | VARCHAR(32) | yes |  |
| `confidence` | VARCHAR(16) | no |  |

**Unique constraints:** `uq_val_ticker_date` (ticker, date)

**Indexes:** `ix_val_ticker_date` (ticker, date), `ix_valuation_snapshots_ticker` (ticker)

---

## `watchlist_items`

Core / swing / short / special-situation lists with notes.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `list_name` | VARCHAR(48) | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `note` | TEXT | yes |  |
| `target_price` | FLOAT | yes |  |
| `added_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `uq_watchlist_item` (list_name, ticker)

**Indexes:** `ix_watchlist_items_list_name` (list_name), `ix_watchlist_items_ticker` (ticker)

---

