"""Research pipeline — orchestrates the agents and persists the output.

The sequence matters and mirrors how a real desk works: the analysts research
independently, the bear analyst then attacks the assembled bull case, and only
then does the Portfolio Manager decide with all of it in front of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytics.quant import analyze_universe
from backend.analytics.service import (
    StockAnalysis,
    analyze_stock,
    build_peer_metrics,
    build_universe_metrics,
    load_price_series,
    persist_score,
)
from backend.core.config import get_settings
from backend.core.data_quality import Claim
from backend.core.logging_config import EVENT_RESEARCH_RUN, get_logger, log_event
from backend.data.models import Position
from backend.data.universe import get_universe
from backend.research.agents import AgentOutput, PortfolioDecision, all_agents
from backend.research.evidence import EvidencePack, build_evidence_pack
from backend.research.llm import CallBudget, LlmClient
from backend.research.thesis import ThesisBundle, upsert_thesis

logger = get_logger(__name__)


@dataclass
class ResearchResult:
    ticker: str
    analysis: StockAnalysis
    pack: EvidencePack
    agent_outputs: dict[str, AgentOutput]
    decision: PortfolioDecision
    bundle: ThesisBundle | None = None
    error: str | None = None

    @property
    def used_llm(self) -> bool:
        return any(o.generated_by == "llm" for o in self.agent_outputs.values())

    @property
    def validation_warnings(self) -> list[str]:
        return [
            f"{name}: {w}"
            for name, output in self.agent_outputs.items()
            for w in output.validation_warnings
        ]

    def to_dict(self, *, include_analysis: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ticker": self.ticker,
            "decision": self.decision.to_dict(),
            "agents": {k: v.to_dict() for k, v in self.agent_outputs.items()},
            "evidence": self.pack.to_dict(),
            "used_llm": self.used_llm,
            "validation_warnings": self.validation_warnings,
            "thesis": self.bundle.thesis.to_dict() if self.bundle else None,
            "change_summary": self.bundle.change_summary if self.bundle else None,
            "error": self.error,
        }
        if include_analysis:
            payload["analysis"] = self.analysis.to_dict()
        return payload


@dataclass
class ResearchRun:
    as_of: date
    results: list[ResearchResult] = field(default_factory=list)
    llm_calls: int = 0
    used_llm: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def new_theses(self) -> list[ResearchResult]:
        return [r for r in self.results if r.bundle and r.bundle.is_new]

    @property
    def updated_theses(self) -> list[ResearchResult]:
        return [r for r in self.results if r.bundle and not r.bundle.is_new]

    def by_action(self, action: str) -> list[ResearchResult]:
        return [r for r in self.results if r.decision.action == action]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "count": len(self.results),
            "llm_calls": self.llm_calls,
            "used_llm": self.used_llm,
            "new_theses": len(self.new_theses),
            "updated_theses": len(self.updated_theses),
            "actions": {
                action: len(self.by_action(action))
                for action in ("BUY", "HOLD", "SELL", "WATCH")
            },
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
        }


def research_stock(
    session: Session,
    analysis: StockAnalysis,
    *,
    agents: dict[str, Any] | None = None,
    currently_held: bool = False,
    portfolio_context: str | None = None,
    persist: bool = True,
    as_of: date | None = None,
) -> ResearchResult:
    """Run the full agent pipeline on one already-computed analysis."""
    agents = agents or all_agents()
    pack = build_evidence_pack(analysis)

    fundamental = agents["fundamental"].run(pack)
    technical = agents["technical"].run(pack)
    event = agents["event"].run(pack)

    # The bear analyst attacks the assembled bull case, not a blank page.
    bull_case = "\n".join(
        [s.text for s in fundamental.statements if s.claim in (Claim.FACT, Claim.INFERENCE)][:10]
        + [s.text for s in technical.statements if s.claim is Claim.INFERENCE][:4]
    )
    bear = agents["bear"].run(pack, bull_case=bull_case, direction="LONG")

    decision = agents["portfolio_manager"].decide(
        pack,
        fundamental=fundamental, technical=technical, event=event, bear=bear,
        currently_held=currently_held, portfolio_context=portfolio_context,
    )

    outputs = {
        "fundamental": fundamental, "technical": technical,
        "event": event, "bear": bear,
    }
    if decision.output:
        outputs["portfolio_manager"] = decision.output

    result = ResearchResult(
        ticker=analysis.ticker, analysis=analysis, pack=pack,
        agent_outputs=outputs, decision=decision,
    )

    if persist:
        result.bundle = upsert_thesis(
            session, analysis, pack, decision, outputs, as_of=as_of or analysis.as_of
        )
    return result


def run_research(
    session: Session,
    tickers: Sequence[str] | None = None,
    *,
    index: str = "egx30",
    as_of: date | None = None,
    persist: bool = True,
    limit: int | None = None,
    min_score: float | None = None,
) -> ResearchRun:
    """Run research across a set of names.

    The cross-section is computed once for the whole universe before any single
    name is researched, because factor scores are only meaningful relative to
    the full peer group.
    """
    as_of = as_of or date.today()
    settings = get_settings()
    run = ResearchRun(as_of=as_of)

    companies = get_universe(session, index)
    if tickers:
        wanted = {t.upper() for t in tickers}
        companies = [c for c in companies if c.ticker in wanted]
    if not companies:
        run.errors.append("No companies matched the requested universe.")
        return run

    universe_tickers = [c.ticker for c in get_universe(session, index)]
    sector_map = {c.ticker: c.sector or "Unknown" for c in get_universe(session, index)}
    universe_metrics = build_universe_metrics(session, universe_tickers, as_of=as_of)
    quant_snapshots = analyze_universe(universe_metrics)
    benchmark = load_price_series(session, settings.benchmark_ticker, as_of=as_of)

    held = set(session.execute(select(Position.ticker)).scalars().all())

    client = LlmClient(budget=CallBudget(limit=settings.ai_max_calls_per_run))
    agents = all_agents(client)

    # Score every candidate first so a min_score filter does not waste agent runs.
    analyses: list[StockAnalysis] = []
    for company in companies:
        try:
            analysis = analyze_stock(
                session, company.ticker, as_of=as_of,
                quant_snapshot=quant_snapshots.get(company.ticker),
                peer_metrics=build_peer_metrics(universe_metrics, sector_map, company.ticker),
                benchmark_series=benchmark if len(benchmark) else None,
            )
        except Exception as exc:  # noqa: BLE001 - one bad name must not stop the run
            run.errors.append(f"{company.ticker}: analysis failed — {exc}")
            continue
        if persist:
            persist_score(session, analysis)
        if min_score is not None and (
            analysis.alpha.value is None or analysis.alpha.value < min_score
        ):
            continue
        analyses.append(analysis)

    analyses.sort(key=lambda a: a.alpha.value or 0.0, reverse=True)
    if limit:
        analyses = analyses[:limit]

    for analysis in analyses:
        try:
            result = research_stock(
                session, analysis, agents=agents,
                currently_held=analysis.ticker in held,
                persist=persist, as_of=as_of,
            )
            run.results.append(result)
        except Exception as exc:  # noqa: BLE001
            run.errors.append(f"{analysis.ticker}: research failed — {exc}")

    run.llm_calls = client.budget.used
    run.used_llm = any(r.used_llm for r in run.results)
    if persist:
        session.flush()

    log_event(
        logger, EVENT_RESEARCH_RUN,
        f"Research run complete: {len(run.results)} names, {len(run.new_theses)} new "
        f"theses, {len(run.updated_theses)} updated, {run.llm_calls} LLM calls",
        as_of=as_of.isoformat(), generated_by="llm" if run.used_llm else "deterministic",
        errors=len(run.errors),
    )
    return run
