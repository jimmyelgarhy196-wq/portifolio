#!/usr/bin/env python
"""Import the Egyptian Exchange's published end-of-day bulletin.

EGX publishes daily trading data on egx.com.eg. Download it, then:

    python scripts/import_egx_bulletin.py --file ~/Downloads/EGX_2026-09-22.xlsx

Or, once the server can reach EGX, fetch it directly:

    python scripts/import_egx_bulletin.py --url https://www.egx.com.eg/<path>

Start with --dry-run. It prints the column mapping it worked out, a sample of
parsed rows, and everything it rejected with a reason, and writes nothing. Only
run the real import once the mapping looks right — a bulletin read through the
wrong column is worse than no bulletin, because it looks fine.

If a column is matched wrongly (or not at all), fix it with a mapping file
rather than by editing code:

    echo '{"close": "Closing Price", "ticker": "Reuters Code"}' > egx-map.json
    python scripts/import_egx_bulletin.py --file b.xlsx --map egx-map.json

Exit codes: 0 imported (or a clean dry run), 1 parsed but nothing stored, 2 the
file could not be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import get_settings  # noqa: E402
from backend.core.database import session_scope  # noqa: E402
from backend.data.providers.base import ProviderError  # noqa: E402
from backend.data.providers.egx_bulletin import (  # noqa: E402
    FIELD_ALIASES, ingest_bulletin, parse_bulletin,
)

RULE = "-" * 74


def fetch(url: str) -> tuple[bytes, str]:
    from backend.data.providers.http_client import HttpFetcher

    fetcher = HttpFetcher()
    response = fetcher.get(url)
    name = url.rsplit("/", 1)[-1] or "bulletin"
    return response.content, name


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="Path to a downloaded EGX bulletin (.csv or .xlsx)")
    src.add_argument("--url", help="URL of the bulletin to download")
    ap.add_argument("--date", help="Trading date of this bulletin (YYYY-MM-DD)")
    ap.add_argument("--map", dest="map_path", help="JSON file of field -> column overrides")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report; write nothing")
    ap.add_argument("--allow-unknown-tickers", action="store_true",
                    help="Store codes that are not in the covered universe")
    ap.add_argument("--sample", type=int, default=8, help="Rows to print (default 8)")
    args = ap.parse_args()

    # Settings supply the defaults so a daily cron needs no arguments.
    settings = get_settings()
    if not args.file and not args.url:
        args.url = settings.bulletin_url
    if not args.map_path and settings.bulletin_map_path:
        args.map_path = settings.bulletin_map_path

    overrides = {}
    if args.map_path:
        try:
            overrides = json.loads(Path(args.map_path).read_text())
        except (OSError, ValueError) as exc:
            print(f"Could not read mapping file: {exc}")
            return 2

    bulletin_date = None
    if args.date:
        try:
            bulletin_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"--date must be YYYY-MM-DD, got {args.date!r}")
            return 2

    if not args.file and not args.url:
        print("Give --file <downloaded bulletin> or --url, or set "
              "EGX_BULLETIN_URL in your environment.")
        return 2

    print("=" * 74)
    print("EGX bulletin import" + ("  (dry run — nothing will be written)" if args.dry_run else ""))
    print("=" * 74)

    # --- read ---------------------------------------------------------------
    try:
        if args.url:
            print(f"\n  Downloading {args.url}")
            payload, name = fetch(args.url)
            print(f"  {len(payload):,} bytes")
            result = parse_bulletin(payload, filename=name, overrides=overrides,
                                    bulletin_date=bulletin_date,
                                    source_label=f"EGX:bulletin:{args.url}")
        else:
            path = Path(args.file).expanduser()
            if not path.exists():
                print(f"\n  File not found: {path}")
                return 2
            print(f"\n  Reading {path}  ({path.stat().st_size:,} bytes)")
            result = parse_bulletin(path, overrides=overrides, bulletin_date=bulletin_date)
    except ProviderError as exc:
        print(f"\nCOULD NOT READ THE BULLETIN\n\n  {exc}\n")
        return 2

    # --- report the mapping -------------------------------------------------
    print(f"\n  Trading date      {result.bulletin_date or 'UNKNOWN'}  (from {result.date_origin})")
    print(f"  Source stamp      {result.source}")
    print(f"\n  COLUMN MAPPING — check this before importing")
    print(f"  {RULE}")
    for field_name in FIELD_ALIASES:
        col = result.mapping.get(field_name)
        mark = " " if col else "·"
        print(f"  {mark} {field_name:<16} {col or '— not present —'}")
    if result.unmapped_headers:
        print(f"\n  Columns ignored:  {', '.join(result.unmapped_headers[:12])}"
              + (" …" if len(result.unmapped_headers) > 12 else ""))

    # --- sample -------------------------------------------------------------
    if result.rows:
        print(f"\n  PARSED ROWS (first {min(args.sample, len(result.rows))} of {len(result.rows)})")
        print(f"  {RULE}")
        print(f"  {'TICKER':<10}{'CLOSE':>11}{'PREV':>11}{'HIGH':>11}{'LOW':>11}{'VOLUME':>14}")
        for row in result.rows[:args.sample]:
            def col(v, width=11, dp=2):
                return f"{v:>{width},.{dp}f}" if v is not None else f"{'—':>{width}}"
            print(f"  {row.ticker:<10}{col(row.close)}{col(row.previous_close)}"
                  f"{col(row.high)}{col(row.low)}{col(row.volume, 14, 0)}")

    if result.rejected:
        print(f"\n  REJECTED ({len(result.rejected)}) — these are not stored")
        print(f"  {RULE}")
        reasons: dict[str, list[str]] = {}
        for label, reason in result.rejected:
            reasons.setdefault(reason, []).append(label)
        for reason, labels in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            shown = ", ".join(labels[:10]) + (" …" if len(labels) > 10 else "")
            print(f"  {len(labels):>4}  {reason}: {shown}")

    print(f"\n  {result.summary()}")

    if not result.rows:
        print("\n  Nothing usable in this file. Check the mapping above.\n")
        return 1

    if args.dry_run:
        print("\n  Dry run — nothing written. Re-run without --dry-run to import.\n")
        return 0

    if result.bulletin_date is None:
        print("\n  Refusing to import: the bulletin has no date. Pass --date YYYY-MM-DD.\n")
        return 1

    # --- store --------------------------------------------------------------
    print(f"\n  Writing to {settings.database_url}")
    try:
        with session_scope() as session:
            counts = ingest_bulletin(
                session, result, only_known_tickers=not args.allow_unknown_tickers
            )
    except ProviderError as exc:
        print(f"\n  IMPORT REFUSED: {exc}\n")
        return 1

    print(f"  inserted {counts['inserted']}, updated {counts['updated']}")
    if counts["unknown_ticker"]:
        print(f"  skipped {counts['unknown_ticker']} code(s) not in the covered universe "
              f"(use --allow-unknown-tickers to store them)")
    print(f"\n  RESULT: bulletin for {result.bulletin_date} imported.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
