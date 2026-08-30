#!/usr/bin/env python3
"""Ingest market data, fundamentals, news and disclosures."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.data.ingestion import (  # noqa: E402
    compute_valuation_snapshot,
    ingest_disclosures,
    ingest_fundamentals,
    ingest_news,
    ingest_prices,
)
from backend.data.providers.registry import (  # noqa: E402
    ProviderChain,
    create_provider,
    provider_status,
)
from backend.data.universe import get_universe  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", default="all",
        choices=["all", "prices", "fundamentals", "news", "disclosures", "valuation"],
    )
    parser.add_argument("--provider", help="force a single provider by name")
    parser.add_argument("--index", default="all", help="egx30 | egx70 | egx100 | all")
    parser.add_argument("--tickers", help="comma-separated tickers")
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--dry-run", action="store_true", help="report provider status only")
    args = parser.parse_args()

    configure_logging()
    init_database()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    if args.dry_run:
        print(f"{'DATASET':<14}{'PROVIDER':<12}{'AVAILABLE':<11}DETAIL")
        print("-" * 78)
        for row in provider_status():
            print(
                f"{row['dataset']:<14}{row['provider']:<12}"
                f"{str(row['available']):<11}{(row['reason'] or row['notes'])[:44]}"
            )
        return 0

    def chain_for(dataset: str) -> ProviderChain | None:
        if not args.provider:
            return None
        provider = create_provider(args.provider)
        if provider is None:
            print(f"Provider {args.provider!r} could not be constructed.", file=sys.stderr)
            raise SystemExit(2)
        return ProviderChain([provider], dataset)

    datasets = (
        ["prices", "fundamentals", "news", "disclosures", "valuation"]
        if args.dataset == "all" else [args.dataset]
    )

    for dataset in datasets:
        print(f"\n=== {dataset.upper()} ===")
        if dataset == "valuation":
            with session_scope() as session:
                names = tickers or [c.ticker for c in get_universe(session, args.index)]
                made = sum(
                    1 for t in names if compute_valuation_snapshot(session, t) is not None
                )
            print(f"  {made} valuation snapshot(s) computed from stored data")
            continue

        with session_scope() as session:
            if dataset == "prices":
                summary = ingest_prices(
                    session, tickers=tickers, index=args.index,
                    lookback_days=args.lookback_days, chain=chain_for("prices"),
                )
            elif dataset == "fundamentals":
                summary = ingest_fundamentals(
                    session, tickers=tickers, index=args.index,
                    chain=chain_for("fundamentals"),
                )
            elif dataset == "news":
                summary = ingest_news(session, tickers=tickers, chain=chain_for("news"))
            else:
                summary = ingest_disclosures(
                    session, tickers=tickers, chain=chain_for("disclosures")
                )

            print(
                f"  +{summary.inserted} new, ~{summary.updated} updated, "
                f"{summary.skipped} skipped, {len(summary.failures)} failed"
            )
            for failure in summary.failures[:5]:
                print(f"  ✗ {failure.ticker or dataset}: {failure.message[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
