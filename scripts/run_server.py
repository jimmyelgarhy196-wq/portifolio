#!/usr/bin/env python3
"""Start the GMG terminal."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument(
        "--scheduler-only", action="store_true",
        help="run the scheduled jobs with no HTTP server (the scheduler container)",
    )
    args = parser.parse_args()

    if args.scheduler_only:
        from backend.core.logging_config import configure_logging
        from backend.jobs.scheduler import run_forever

        configure_logging()
        return run_forever()

    import uvicorn

    print(f"\n  GMG  ·  http://{args.host}:{args.port}")
    print(f"  API docs   ·  http://{args.host}:{args.port}/api/docs")
    if settings.allow_synthetic_data:
        print("\n  ⚠  SYNTHETIC DATA ENABLED — displayed figures may be fictional.")
    if not settings.ai_enabled:
        print("\n  ℹ  No ANTHROPIC_API_KEY — research uses the deterministic engine.")
    print()

    uvicorn.run(
        "backend.api.app:app",
        host=args.host, port=args.port, reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
