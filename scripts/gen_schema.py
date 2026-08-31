#!/usr/bin/env python3
"""Regenerate database/SCHEMA.md from the SQLAlchemy models.

    python scripts/gen_schema.py > database/SCHEMA.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect  # noqa: E402

from backend.core.database import get_engine, init_database  # noqa: E402
from backend.data import models  # noqa: F401,E402

DESCRIPTIONS = {
    "companies": "The EGX universe: ticker, name, sector, index membership and provider symbol mapping. Reference data only — no prices or financials.",
    "price_history": "Daily OHLCV bars. A unique constraint on (ticker, timestamp) is the primary defence against double ingestion. Market holidays are simply absent rows; no bar is ever synthesised.",
    "financial_statements": "Reported financials. `available_from` records when a statement was actually published — the backtester filters on it, not on `period_end`, which is what prevents look-ahead bias.",
    "valuation_snapshots": "Multiples derived from price + fundamentals + share count. Every field is a CALCULATION; any multiple whose inputs are missing stays NULL.",
    "news": "News items with lexicon sentiment. `sentiment` is NULL when no lexicon term matched — 'no signal' and 'neutral' are different claims.",
    "disclosures": "Official corporate disclosures, classified by type and importance. These drive the catalyst score.",
    "research_theses": "Durable investment theses. One per (ticker, direction), updated weekly rather than replaced.",
    "thesis_versions": "Immutable snapshots of prior thesis states. APPEND-ONLY — this is what makes 'what changed since last week?' answerable from the record.",
    "recommendations": "Every recommendation ever made. APPEND-ONLY, so model evaluation measures what the system said at the time rather than a reconstruction.",
    "score_history": "Score per ticker per date. APPEND-ONLY, powering week-over-week deltas and score-change alerts.",
    "portfolios": "Paper portfolios. `mode` is always PAPER; the column exists so a future live mode would be explicit rather than implicit.",
    "positions": "Open positions, long and paper-short.",
    "trades": "Immutable paper-trade ledger with commission and slippage on every fill.",
    "portfolio_snapshots": "Daily valuations. Volatility, beta, drawdown and VaR all depend on this history existing.",
    "watchlist_items": "Core / swing / short / special-situation lists with notes.",
    "alerts": "Generated alerts. Notifications are dispatched only when a channel is explicitly configured.",
    "reports": "Stored weekly investment committee reports, as markdown and as structured sections so they can be compared programmatically.",
    "backtest_runs": "Backtest configurations, metrics, equity curves and trades.",
    "settings": "User settings overriding YAML and environment defaults.",
    "data_quality_log": "Audit trail of every ingestion run: provider, status, rows in, rows skipped, and the reason for any failure.",
}

PROVENANCE = {"source", "retrieved_at", "data_period", "confidence"}

HEADER = """# GMG — Database Schema

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
"""


def main() -> int:
    init_database()
    insp = inspect(get_engine())
    print(HEADER)

    for table in sorted(insp.get_table_names()):
        columns = insp.get_columns(table)
        names = {c["name"] for c in columns}
        marker = "  **(provenance)**" if PROVENANCE <= names else ""
        print(f"## `{table}`{marker}\n")
        if table in DESCRIPTIONS:
            print(f"{DESCRIPTIONS[table]}\n")

        print("| Column | Type | Null | Default |")
        print("|---|---|---|---|")
        for column in columns:
            default = column.get("default")
            default = "" if default is None else f"`{default}`"
            print(
                f"| `{column['name']}` | {column['type']} | "
                f"{'yes' if column['nullable'] else 'no'} | {default} |"
            )
        print()

        uniques = insp.get_unique_constraints(table)
        if uniques:
            print("**Unique constraints:** " + ", ".join(
                f"`{u['name']}` ({', '.join(u['column_names'])})" for u in uniques
            ) + "\n")
        indexes = insp.get_indexes(table)
        if indexes:
            print("**Indexes:** " + ", ".join(
                f"`{i['name']}` ({', '.join(i['column_names'])})" for i in indexes
            ) + "\n")
        fks = insp.get_foreign_keys(table)
        if fks:
            print("**Foreign keys:** " + ", ".join(
                f"{', '.join(f['constrained_columns'])} → {f['referred_table']}."
                f"{', '.join(f['referred_columns'])}" for f in fks
            ) + "\n")
        print("---\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
