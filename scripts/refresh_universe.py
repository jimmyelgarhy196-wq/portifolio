#!/usr/bin/env python3
"""Reconcile the EGX universe against an official constituent list.

The shipped universe is a reference seed flagged unverified. Download the current
constituent lists from https://www.egx.com.eg and pass them here to replace it.

Expected CSV columns (case-insensitive, extras ignored):

    ticker,name,sector,industry,in_egx30,in_egx70,shares_outstanding

Boolean columns accept true/false, yes/no, 1/0.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import CONFIG_DIR  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.data.models import Company  # noqa: E402

TRUE = {"true", "yes", "y", "1", "t"}


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-csv", required=True, help="path to the constituent CSV")
    parser.add_argument(
        "--mark-verified", action="store_true",
        help="set meta.verified=true in config/universe.yaml after a successful import",
    )
    args = parser.parse_args()

    configure_logging()
    init_database()
    path = Path(args.from_csv)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    created = updated = 0
    from sqlalchemy import select

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = [
            {(k or "").strip().lower(): v for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]

    with session_scope() as session:
        for row in rows:
            ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
            if not ticker:
                continue
            company = session.scalar(select(Company).where(Company.ticker == ticker))
            fields = {
                "name": row.get("name") or ticker,
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "in_egx30": parse_bool(row.get("in_egx30")),
                "in_egx70": parse_bool(row.get("in_egx70")),
            }
            fields["in_egx100"] = (
                parse_bool(row.get("in_egx100")) or fields["in_egx30"] or fields["in_egx70"]
            )
            shares = row.get("shares_outstanding")
            if shares:
                try:
                    fields["shares_outstanding"] = float(str(shares).replace(",", ""))
                except ValueError:
                    pass

            if company is None:
                session.add(Company(
                    ticker=ticker, exchange="EGX", currency="EGP",
                    provider_symbols={"yahoo": f"{ticker}.CA"}, **fields,
                ))
                created += 1
            else:
                for key, value in fields.items():
                    if value is not None:
                        setattr(company, key, value)
                updated += 1

    print(f"Universe reconciled from {path.name}: {created} created, {updated} updated")

    if args.mark_verified:
        universe_path = CONFIG_DIR / "universe.yaml"
        text = universe_path.read_text(encoding="utf-8")
        text = text.replace("  verified: false", "  verified: true")
        text = text.replace("  as_of: null", f"  as_of: {date.today().isoformat()}")
        text = text.replace(
            '  source: "seed:general-knowledge"', f'  source: "official:{path.name}"'
        )
        universe_path.write_text(text, encoding="utf-8")
        print("config/universe.yaml marked verified. The UI warning will disappear on restart.")
    else:
        print(
            "\nNote: config/universe.yaml is still flagged unverified and the UI warning\n"
            "remains. Pass --mark-verified once you are satisfied the import is correct."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
