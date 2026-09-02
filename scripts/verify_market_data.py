#!/usr/bin/env python
"""Prove a live market-data feed works — before trusting a single price.

Run this the moment you have a vendor key. It calls the vendor exactly the way
the platform will, prints the raw response beside the parsed quote, and tells
you plainly whether the feed is usable. It writes nothing to the database, so
it is safe to run against production configuration.

    python scripts/verify_market_data.py --tickers COMI,HRHO,SWDY

Exit codes: 0 every requested ticker returned a usable quote; 1 some did; 2 the
feed is not usable at all. That makes it a deployment gate, not just a report.

If a ticker comes back empty, the answer is almost always the symbol: vendors
disagree about EGX symbology. Use --raw to see what the vendor actually sent,
then add the correct symbol to config/symbol_map.json. No code change.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import get_settings  # noqa: E402
from backend.data.providers.base import ProviderError  # noqa: E402
from backend.market.live_providers import (  # noqa: E402
    PRESETS, RestQuoteProvider, SymbolMapper, load_vendor_spec,
)
from backend.market.quotes import LicensedQuoteProvider  # noqa: E402

DEFAULT_TICKERS = "COMI,HRHO,SWDY,ETEL,EAST"


def line(char: str = "-", width: int = 74) -> None:
    print(char * width)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=DEFAULT_TICKERS,
                    help=f"Comma-separated EGX tickers (default: {DEFAULT_TICKERS})")
    ap.add_argument("--raw", action="store_true",
                    help="Print the vendor's raw JSON response")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    settings = get_settings()

    line("=")
    print("GMG market-data feed verification")
    line("=")

    # --- configuration -----------------------------------------------------
    try:
        spec = load_vendor_spec()
    except ProviderError as exc:
        print(f"\nCONFIGURATION ERROR\n  {exc}\n")
        return 2

    if spec is None:
        print("\nNO VENDOR CONFIGURED — the platform is not live.\n")
        print("  Set one of these in your .env:")
        print(f"    EGX_MARKET_DATA_VENDOR=<{ '|'.join(sorted(PRESETS)) }>")
        print("    EGX_MARKET_DATA_SPEC_PATH=/path/to/vendor-spec.json   (any other vendor)")
        print("  ...and:")
        print("    EGX_MARKET_DATA_API_KEY=<your key>")
        print("    EGX_QUOTE_DELAY_MINUTES=0     # 0 real-time, 15 delayed — what you pay for\n")
        return 2

    key = settings.market_data_api_key
    print(f"\n  Vendor            {spec.display_name}  ({spec.name})")
    print(f"  Endpoint          {spec.url}")
    print(f"  API key           {'set (' + str(len(key)) + ' chars)' if key else 'MISSING'}")
    print(f"  Symbol pattern    TICKER{spec.symbol_suffix}")
    print(f"  Licence delay     {settings.quote_delay_minutes} min"
          f"{'  (real-time)' if settings.quote_delay_minutes == 0 else ''}")
    if spec.docs:
        print(f"  Vendor docs       {spec.docs}")

    if not key:
        print("\n  Cannot test without EGX_MARKET_DATA_API_KEY.\n")
        return 2

    mapper = SymbolMapper.load(spec)
    print(f"\n  Requesting: {', '.join(mapper.to_vendor(t) for t in tickers)}")

    # --- raw call ----------------------------------------------------------
    client = RestQuoteProvider(spec, key, mapper=mapper)
    if args.raw:
        line()
        url, params = client._url_and_params([mapper.to_vendor(t) for t in tickers])
        shown = {k: ("***" if k == spec.auth_param else v) for k, v in params.items()}
        print(f"GET {url}\n    params {shown}")
        try:
            payload = client._fetcher.get_json(url, params=params)
            print(json.dumps(payload, indent=2)[:4000])
        except ProviderError as exc:
            print(f"    FAILED: {exc}")

    # --- through the real provider ----------------------------------------
    line()
    provider = LicensedQuoteProvider(client=client)
    try:
        quotes = provider.get_quotes(tickers)
    except ProviderError as exc:
        print(f"\nFEED ERROR\n  {exc}\n")
        print("  Nothing was written. The platform would show the last known")
        print("  quote with its true age, or 'N/A — data unavailable'.\n")
        return 2

    if not quotes:
        print("\nNO USABLE QUOTES RETURNED.\n")
        print("  The call succeeded but no row carried a usable price. Common causes:")
        print("   * the symbol format is wrong  -> re-run with --raw, then fix")
        print("     config/symbol_map.json")
        print("   * your plan does not cover the Egyptian Exchange")
        print("   * the market has never traded these symbols on this vendor\n")
        return 2

    header = f"{'TICKER':<8}{'PRICE':>11}{'PREV':>11}{'CHANGE':>10}{'VOLUME':>14}  QUOTE TIME"
    print(f"\n{header}")
    line()
    now = datetime.now(timezone.utc)
    for ticker in tickers:
        q = quotes.get(ticker)
        if q is None:
            print(f"{ticker:<8}{'not covered by this vendor':>46}")
            continue
        chg = f"{q.change_pct * 100:+.2f}%" if q.change_pct is not None else "—"
        prev = f"{q.previous_close:,.2f}" if q.previous_close is not None else "—"
        vol = f"{q.volume:,.0f}" if q.volume is not None else "—"
        if q.quote_time:
            age = (now - q.quote_time).total_seconds() / 60.0
            stamp = f"{q.quote_time:%Y-%m-%d %H:%M}Z  ({age:,.0f} min ago)"
        else:
            stamp = "no timestamp from vendor"
        print(f"{ticker:<8}{q.price:>11,.2f}{prev:>11}{chg:>10}{vol:>14}  {stamp}")

    covered = len(quotes)
    line()
    print(f"\n  {covered} of {len(tickers)} tickers returned a usable quote.")

    demo = [t for t, q in quotes.items() if q.is_demo]
    print(f"  Labelled demo:    {len(demo)}  (must be 0 on a licensed feed)")
    print(f"  Reported delay:   {provider.delayed_minutes} min — shown to users verbatim")

    stale = [
        t for t, q in quotes.items()
        if q.quote_time and (now - q.quote_time).total_seconds() > 86400
    ]
    if stale:
        print(f"\n  WARNING: {len(stale)} quote(s) are over a day old: {', '.join(sorted(stale))}")
        print("  That is end-of-day data, not real-time. Check your plan before")
        print("  setting EGX_QUOTE_DELAY_MINUTES=0.")

    if settings.quote_delay_minutes == 0 and not stale:
        print("\n  Configured as REAL-TIME. Confirm your licence actually grants")
        print("  real-time redistribution to your subscribers before going live.")

    print()
    if covered == len(tickers):
        print("  RESULT: feed usable. Start the app and quotes will be live.\n")
        return 0
    print("  RESULT: feed partially usable. Fix the missing symbols in")
    print("  config/symbol_map.json — uncovered tickers will show 'N/A'.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
