#!/usr/bin/env python3
"""Create the database schema and load the EGX universe."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.data.universe import load_universe, universe_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="DROP all tables first")
    parser.add_argument("--no-universe", action="store_true", help="skip loading the universe")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    if args.reset:
        answer = input(
            f"This will DROP every table in {settings.database_url}.\n"
            "All stored prices, theses, trades and reports will be lost. Type 'yes': "
        )
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    init_database(drop_all=args.reset)
    print(f"Schema created in {settings.database_url}")

    if not args.no_universe:
        with session_scope() as session:
            result = load_universe(session)
        with session_scope() as session:
            status = universe_status(session)
        print(f"Universe: {result['created']} created, {result['updated']} updated")
        if status.warning:
            print(f"\n⚠  {status.warning}\n")

    print("\nNext steps:")
    print("  python scripts/ingest.py --dataset all     # load real market data")
    print("  python scripts/run_server.py               # start the terminal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
