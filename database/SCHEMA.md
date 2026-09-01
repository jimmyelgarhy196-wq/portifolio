# GMG — Database Schema

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
predictions depends on that history being immutable. `audit_log`, `email_log`
and `login_attempts` are append-only for the same reason: they are the record
of what happened, not a mutable view of the present.

## Secrets

No table stores a plaintext password, a usable session token, or a card number.
`users.password_hash` is an Argon2id hash; `user_sessions.token_hash` and
`one_time_tokens.token_hash` are SHA-256 digests of values held only by the
client. There is no card-handling column anywhere, because there is no
card-handling code.

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

## `audit_logs`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | yes |  |
| `actor_email` | VARCHAR(255) | yes |  |
| `action` | VARCHAR(64) | no |  |
| `target` | VARCHAR(160) | yes |  |
| `ip` | VARCHAR(64) | yes |  |
| `user_agent` | VARCHAR(255) | yes |  |
| `detail` | JSON | no |  |
| `created_at` | DATETIME | no |  |

**Indexes:** `ix_audit_action_time` (action, created_at), `ix_audit_logs_created_at` (created_at), `ix_audit_logs_user_id` (user_id)

**Foreign keys:** user_id → users.id

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

## `data_sources`

Registry of every configured provider with its last success, last error, credential state and whether it serves demonstration data. Backs the public /data-sources page.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `name` | VARCHAR(64) | no |  |
| `kind` | VARCHAR(32) | no |  |
| `enabled` | BOOLEAN | no |  |
| `is_demo` | BOOLEAN | no |  |
| `requires_credentials` | BOOLEAN | no |  |
| `credentials_present` | BOOLEAN | no |  |
| `delayed_minutes` | INTEGER | no |  |
| `last_success_at` | DATETIME | yes |  |
| `last_error_at` | DATETIME | yes |  |
| `last_error` | TEXT | yes |  |
| `notes` | TEXT | yes |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `None` (name)

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

## `email_log`

Every email the system attempted and its outcome, so a delivery failure is visible rather than silent.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | yes |  |
| `to_email` | VARCHAR(255) | no |  |
| `template` | VARCHAR(64) | no |  |
| `subject` | VARCHAR(255) | no |  |
| `provider` | VARCHAR(32) | no |  |
| `status` | VARCHAR(16) | no |  |
| `error` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |

**Indexes:** `ix_email_log_created_at` (created_at), `ix_email_log_template` (template), `ix_email_log_user_id` (user_id), `ix_email_to_time` (to_email, created_at)

**Foreign keys:** user_id → users.id

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

## `login_attempts`

Every sign-in attempt, successful or not. Drives per-email and per-IP lockout.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `email` | VARCHAR(255) | no |  |
| `ip` | VARCHAR(64) | yes |  |
| `successful` | BOOLEAN | no |  |
| `created_at` | DATETIME | no |  |

**Indexes:** `ix_login_attempts_created_at` (created_at), `ix_login_attempts_ip` (ip), `ix_login_email_time` (email, created_at)

---

## `market_quotes`

The quote cache. Each row carries `source`, `quote_time`, `retrieved_at`, `delayed_minutes` and `is_demo`, which travel unchanged into the UI — this is what makes a price's freshness impossible to misrepresent.

| Column | Type | Null | Default |
|---|---|---|---|
| `ticker` | VARCHAR(24) | no |  |
| `price` | FLOAT | yes |  |
| `previous_close` | FLOAT | yes |  |
| `change` | FLOAT | yes |  |
| `change_pct` | FLOAT | yes |  |
| `open` | FLOAT | yes |  |
| `day_high` | FLOAT | yes |  |
| `day_low` | FLOAT | yes |  |
| `volume` | FLOAT | yes |  |
| `turnover` | FLOAT | yes |  |
| `trades` | INTEGER | yes |  |
| `week52_high` | FLOAT | yes |  |
| `week52_low` | FLOAT | yes |  |
| `market_cap` | FLOAT | yes |  |
| `currency` | VARCHAR(8) | no |  |
| `quote_time` | DATETIME | yes |  |
| `retrieved_at` | DATETIME | no |  |
| `source` | VARCHAR(64) | no |  |
| `delayed_minutes` | INTEGER | no |  |
| `market_status` | VARCHAR(16) | no |  |
| `is_demo` | BOOLEAN | no |  |

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

## `one_time_tokens`

Email-verification and password-reset tokens, stored hashed and single-use. Issuing a new token for a purpose retires any outstanding one.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `purpose` | VARCHAR(32) | no |  |
| `token_hash` | VARCHAR(64) | no |  |
| `created_at` | DATETIME | no |  |
| `expires_at` | DATETIME | no |  |
| `used_at` | DATETIME | yes |  |
| `meta` | JSON | no |  |

**Unique constraints:** `None` (token_hash)

**Indexes:** `ix_one_time_tokens_user_id` (user_id), `ix_ott_user_purpose` (user_id, purpose)

**Foreign keys:** user_id → users.id

---

## `payments`

Payment records. Rows are created PENDING and only move to SUCCEEDED when a verified gateway webhook, or an audited administrator action, confirms money arrived. Nothing in this codebase charges a card.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `subscription_id` | INTEGER | yes |  |
| `amount_egp` | FLOAT | no |  |
| `currency` | VARCHAR(8) | no |  |
| `status` | VARCHAR(16) | no |  |
| `provider` | VARCHAR(48) | no |  |
| `external_id` | VARCHAR(128) | yes |  |
| `description` | VARCHAR(255) | yes |  |
| `failure_reason` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |
| `settled_at` | DATETIME | yes |  |
| `period_start` | DATE | yes |  |
| `period_end` | DATE | yes |  |
| `meta` | JSON | no |  |

**Indexes:** `ix_payments_created_at` (created_at), `ix_payments_external_id` (external_id), `ix_payments_status` (status), `ix_payments_subscription_id` (subscription_id), `ix_payments_user_id` (user_id), `ix_payments_user_time` (user_id, created_at)

**Foreign keys:** user_id → users.id, subscription_id → subscriptions.id

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

## `saved_screens`

Saved screener criteria, stored as JSON. Unique per (user, name).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `name` | VARCHAR(96) | no |  |
| `filters` | JSON | no |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `uq_screen_user_name` (user_id, name)

**Indexes:** `ix_saved_screens_user_id` (user_id)

**Foreign keys:** user_id → users.id

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

## `subscriptions`

Subscription state. Status alone never grants access: `is_entitled()` also checks `current_period_end`, so an ACTIVE row past its period is denied and a CANCELLED row inside its paid period is honoured.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `plan_code` | VARCHAR(64) | no |  |
| `plan_name` | VARCHAR(128) | no |  |
| `status` | VARCHAR(16) | no |  |
| `price_egp` | FLOAT | no |  |
| `interval` | VARCHAR(16) | no |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |
| `trial_ends_at` | DATETIME | yes |  |
| `current_period_start` | DATETIME | yes |  |
| `current_period_end` | DATETIME | yes |  |
| `cancelled_at` | DATETIME | yes |  |
| `cancel_reason` | TEXT | yes |  |
| `ended_at` | DATETIME | yes |  |
| `external_id` | VARCHAR(128) | yes |  |
| `provider` | VARCHAR(48) | yes |  |
| `meta` | JSON | no |  |

**Indexes:** `ix_subs_user_status` (user_id, status), `ix_subscriptions_created_at` (created_at), `ix_subscriptions_current_period_end` (current_period_end), `ix_subscriptions_external_id` (external_id), `ix_subscriptions_status` (status), `ix_subscriptions_user_id` (user_id)

**Foreign keys:** user_id → users.id

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

## `user_alerts`

Price, percentage-move, RSI, 52-week and moving-average alerts. An alert never fires on demonstration data, and is skipped with a recorded reason when the value it needs is unavailable.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `condition` | VARCHAR(32) | no |  |
| `threshold` | FLOAT | yes |  |
| `note` | TEXT | yes |  |
| `active` | BOOLEAN | no |  |
| `email_delivery` | BOOLEAN | no |  |
| `created_at` | DATETIME | no |  |
| `last_triggered_at` | DATETIME | yes |  |
| `trigger_count` | INTEGER | no |  |
| `last_message` | TEXT | yes |  |

**Indexes:** `ix_user_alerts_ticker` (ticker), `ix_user_alerts_user_id` (user_id), `ix_useralert_active` (user_id, active)

**Foreign keys:** user_id → users.id

---

## `user_portfolio_positions`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `portfolio_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `shares` | FLOAT | no |  |
| `purchase_price` | FLOAT | no |  |
| `purchase_date` | DATE | yes |  |
| `note` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |

**Indexes:** `ix_user_portfolio_positions_portfolio_id` (portfolio_id), `ix_user_portfolio_positions_ticker` (ticker), `ix_userpos_portfolio` (portfolio_id, ticker)

**Foreign keys:** portfolio_id → user_portfolios.id

---

## `user_portfolios`

Subscriber portfolios. A tracking record only: GMG holds no securities and places no orders.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `name` | VARCHAR(96) | no |  |
| `currency` | VARCHAR(8) | no |  |
| `description` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `uq_portfolio_user_name` (user_id, name)

**Indexes:** `ix_user_portfolios_user_id` (user_id)

**Foreign keys:** user_id → users.id

---

## `user_sessions`

Sign-in sessions. Only the SHA-256 digest of the token is stored, so a copy of this table yields no usable session. `epoch` is compared against the user's, so a password change retires every row.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `token_hash` | VARCHAR(64) | no |  |
| `epoch` | INTEGER | no |  |
| `created_at` | DATETIME | no |  |
| `expires_at` | DATETIME | no |  |
| `last_seen_at` | DATETIME | no |  |
| `ip` | VARCHAR(64) | yes |  |
| `user_agent` | VARCHAR(255) | yes |  |
| `revoked_at` | DATETIME | yes |  |

**Unique constraints:** `None` (token_hash)

**Indexes:** `ix_sessions_user_expiry` (user_id, expires_at), `ix_user_sessions_user_id` (user_id)

**Foreign keys:** user_id → users.id

---

## `user_watchlist_items`

Tickers on a watchlist, with an optional note and target price.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `watchlist_id` | INTEGER | no |  |
| `ticker` | VARCHAR(24) | no |  |
| `note` | TEXT | yes |  |
| `target_price` | FLOAT | yes |  |
| `added_at` | DATETIME | no |  |

**Unique constraints:** `uq_watchlist_item` (watchlist_id, ticker)

**Indexes:** `ix_user_watchlist_items_ticker` (ticker), `ix_user_watchlist_items_watchlist_id` (watchlist_id)

**Foreign keys:** watchlist_id → user_watchlists.id

---

## `user_watchlists`

Subscriber watchlists. Unique per (user, name).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `user_id` | INTEGER | no |  |
| `name` | VARCHAR(96) | no |  |
| `description` | TEXT | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |

**Unique constraints:** `uq_watchlist_user_name` (user_id, name)

**Indexes:** `ix_user_watchlists_user_id` (user_id)

**Foreign keys:** user_id → users.id

---

## `users`

Subscriber accounts. `password_hash` is Argon2id; the plaintext never reaches the database, a log or an audit row. `session_epoch` is bumped on a password change, which invalidates every existing session at once.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `email` | VARCHAR(255) | no |  |
| `password_hash` | VARCHAR(255) | no |  |
| `full_name` | VARCHAR(160) | yes |  |
| `role` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `email_verified_at` | DATETIME | yes |  |
| `created_at` | DATETIME | no |  |
| `updated_at` | DATETIME | no |  |
| `last_login_at` | DATETIME | yes |  |
| `last_login_ip` | VARCHAR(64) | yes |  |
| `session_epoch` | INTEGER | no |  |
| `marketing_opt_in` | BOOLEAN | no |  |
| `preferences` | JSON | no |  |

**Unique constraints:** `None` (email)

**Indexes:** `ix_users_created_at` (created_at), `ix_users_email_lower` (email)

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

