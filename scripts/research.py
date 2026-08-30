#!/usr/bin/env python3
"""Run the AI research pipeline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402
from backend.research.pipeline import run_research  # noqa: E402
from backend.research.thesis import render_thesis  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="research a single name")
    parser.add_argument("--index", default="egx30", help="egx30 | egx70 | egx100 | all")
    parser.add_argument("--limit", type=int, help="cap the number of names researched")
    parser.add_argument("--min-score", type=float, help="only research names scoring above this")
    parser.add_argument("--show-thesis", action="store_true", help="print the full thesis")
    args = parser.parse_args()

    configure_logging()
    init_database()
    settings = get_settings()

    print(
        f"Narrative engine: {'LLM (' + settings.ai_model + ')' if settings.ai_enabled else 'deterministic'}"
    )
    if not settings.ai_enabled:
        print("(set ANTHROPIC_API_KEY for LLM narrative; the system is fully functional without it)")

    tickers = [args.ticker.upper()] if args.ticker else None
    with session_scope() as session:
        run = run_research(
            session, tickers=tickers, index="all" if args.ticker else args.index,
            limit=args.limit, min_score=args.min_score,
        )

        print(f"\nResearched {len(run.results)} name(s), {run.llm_calls} LLM call(s)")
        print(
            f"  BUY {len(run.by_action('BUY'))} · HOLD {len(run.by_action('HOLD'))} · "
            f"SELL {len(run.by_action('SELL'))} · WATCH {len(run.by_action('WATCH'))}"
        )
        print(f"  {len(run.new_theses)} new theses, {len(run.updated_theses)} updated")

        if run.errors:
            print("\nErrors:")
            for error in run.errors[:8]:
                print(f"  ✗ {error}")

        warnings = [w for r in run.results for w in r.validation_warnings]
        if warnings:
            print(f"\nValidation warnings ({len(warnings)}):")
            for warning in warnings[:8]:
                print(f"  ⚠ {warning}")

        print(f"\n{'TICKER':<8}{'ACTION':<8}{'CONV':>6}{'SCORE':>7}  {'THESIS':<12}CHANGE")
        print("-" * 78)
        for result in run.results:
            thesis = result.bundle.thesis if result.bundle else None
            change = (result.bundle.change_summary or "").replace("\n", " · ")[:34] if result.bundle else ""
            score = result.analysis.alpha.value
            print(
                f"{result.ticker:<8}{result.decision.action:<8}"
                f"{result.decision.conviction:>6.1f}"
                f"{(f'{score:.0f}' if score is not None else '—'):>7}  "
                f"{(thesis.reference if thesis else '—'):<12}{change}"
            )

        if args.show_thesis and run.results and run.results[0].bundle:
            print("\n" + "=" * 78)
            print(render_thesis(run.results[0].bundle.thesis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
