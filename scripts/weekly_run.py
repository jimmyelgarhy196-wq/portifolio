#!/usr/bin/env python3
"""Run the full weekly research and reporting pipeline."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.api.schemas import uses_synthetic_data  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.jobs.weekly import run_weekly_pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="egx30")
    parser.add_argument("--as-of", help="YYYY-MM-DD (defaults to today)")
    parser.add_argument("--skip-ingestion", action="store_true")
    parser.add_argument("--research-limit", type=int, default=25)
    parser.add_argument("--print-report", action="store_true")
    args = parser.parse_args()

    configure_logging()
    init_database()

    with session_scope() as session:
        synthetic = uses_synthetic_data(session)
    if synthetic:
        print(
            "⚠  This database contains SYNTHETIC data. The report will be generated "
            "for format demonstration only and marked accordingly.\n"
        )

    result = run_weekly_pipeline(
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        index=args.index,
        skip_ingestion=args.skip_ingestion,
        research_limit=args.research_limit,
        acknowledge_synthetic=synthetic,
    )
    print(result.render())

    if args.print_report and result.report_id:
        from backend.data.models import Report

        with session_scope() as session:
            report = session.get(Report, result.report_id)
            if report:
                print("\n" + "=" * 78 + "\n")
                print(report.markdown)

    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
