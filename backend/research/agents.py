"""AI research agents.

Five agents: Fundamental, Technical, Event and Bear analysts, and a Portfolio
Manager who decides.

Each agent has two implementations of the same interface:

* **LLM path** — used when ``ANTHROPIC_API_KEY`` is set. The agent receives only
  the evidence pack and must tag every claim. Its output is validated: numbers
  that do not trace to the evidence are flagged, and badly-formed output is
  rejected in favour of the deterministic path.
* **Deterministic path** — always available. Composes the same analysis directly
  from computed metrics using explicit rules. It cannot hallucinate because it
  never generates language about anything it was not handed.

This is why the system is fully functional with no API key: the LLM improves the
prose, it does not supply the substance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from backend.core.data_quality import Claim, Statement
from backend.core.logging_config import EVENT_RESEARCH_RUN, get_logger, log_event
from backend.research.evidence import (
    EvidencePack,
    find_derived_numbers,
    find_unsupported_numbers,
)
from backend.research.llm import LlmClient, LlmResponse
from backend.research import prompts

logger = get_logger(__name__)

VALID_TAGS = {c.value for c in Claim}


@dataclass
class AgentOutput:
    """One agent's contribution, with provenance and validation results."""

    agent: str
    text: str
    statements: list[Statement] = field(default_factory=list)
    generated_by: str = "deterministic"       # deterministic | llm
    model: str | None = None
    validation_warnings: list[str] = field(default_factory=list)
    unsupported_numbers: list[float] = field(default_factory=list)
    fallback_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        return bool(self.text.strip())

    def claims_of(self, claim: Claim) -> list[Statement]:
        return [s for s in self.statements if s.claim is claim]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "text": self.text,
            "statements": [s.to_dict() for s in self.statements],
            "generated_by": self.generated_by,
            "model": self.model,
            "validation_warnings": self.validation_warnings,
            "unsupported_numbers": self.unsupported_numbers,
            "fallback_reason": self.fallback_reason,
            "claim_counts": {
                c.value: len(self.claims_of(c)) for c in Claim
            },
        }


def parse_statements(text: str) -> list[Statement]:
    """Extract tagged claims from agent output."""
    statements: list[Statement] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        for tag in VALID_TAGS:
            if line.upper().startswith(f"{tag}:"):
                body = line[len(tag) + 1:].strip()
                if body:
                    statements.append(Statement(Claim(tag), body))
                break
    return statements


def validate_output(text: str, pack: EvidencePack) -> tuple[list[str], list[float]]:
    """Check agent output against the evidence pack."""
    warnings: list[str] = []
    statements = parse_statements(text)

    if not statements:
        warnings.append(
            "No tagged claims found. The agent did not follow the "
            "FACT/CALCULATION/INFERENCE/OPINION/UNKNOWN discipline."
        )

    # Strict: a FACT (or an untagged assertion) must restate the evidence.
    unsupported = find_unsupported_numbers(text, pack)
    if unsupported:
        warnings.append(
            f"{len(unsupported)} numeric value(s) asserted as fact do not trace to the "
            "evidence pack: "
            + ", ".join(f"{n:g}" for n in unsupported[:8])
            + ("…" if len(unsupported) > 8 else "")
        )

    # Informational: arithmetic the agent performed, which cannot be verified
    # against the pack by construction.
    derived = find_derived_numbers(text, pack)
    if derived:
        warnings.append(
            f"{len(derived)} figure(s) on CALCULATION lines are derived arithmetic and "
            "were not verified against the evidence pack: "
            + ", ".join(f"{n:g}" for n in derived[:8])
        )

    # A FACT must restate the evidence. Facts asserting unavailable data are the
    # exact failure mode this whole architecture exists to catch.
    unknown_labels = {u.lower() for u in pack.unknowns}
    for statement in statements:
        if statement.claim is Claim.FACT:
            for label in unknown_labels:
                head = label.split("(")[0].strip().lower()
                if len(head) > 8 and head in statement.text.lower():
                    warnings.append(
                        f"A FACT was asserted about '{head}', which the evidence "
                        "pack lists as unavailable."
                    )
                    break
    return warnings, unsupported


class BaseAgent:
    """Common LLM-with-deterministic-fallback behaviour."""

    name = "agent"
    system_prompt = ""
    categories: tuple[str, ...] = ()

    def __init__(self, client: LlmClient | None = None) -> None:
        self.client = client or LlmClient()

    def run(self, pack: EvidencePack, **context: Any) -> AgentOutput:
        if self.client.available:
            output = self._run_llm(pack, **context)
            if output is not None:
                return output
        return self._run_deterministic(
            pack,
            fallback_reason=self.client.unavailable_reason
            or "LLM unavailable or output rejected by validation.",
            **context,
        )

    # -- LLM path -------------------------------------------------------------
    def _run_llm(self, pack: EvidencePack, **context: Any) -> AgentOutput | None:
        prompt = self.build_prompt(pack, **context)
        response: LlmResponse = self.client.complete(self.system_prompt, prompt)
        if not response.used_llm or not response.text.strip():
            log_event(
                logger, EVENT_RESEARCH_RUN,
                f"{self.name}: falling back to deterministic composition",
                agent=self.name, reason=response.error,
            )
            return None

        warnings, unsupported = validate_output(response.text, pack)
        statements = parse_statements(response.text)

        # Untagged output is not usable: the discipline is the safeguard.
        if not statements:
            log_event(
                logger, EVENT_RESEARCH_RUN,
                f"{self.name}: LLM output rejected (no tagged claims)",
                agent=self.name,
            )
            return None

        return AgentOutput(
            agent=self.name, text=response.text.strip(), statements=statements,
            generated_by="llm", model=response.model,
            validation_warnings=warnings, unsupported_numbers=unsupported,
        )

    def build_prompt(self, pack: EvidencePack, **context: Any) -> str:
        return (
            "EVIDENCE\n"
            "========\n"
            f"{pack.render(categories=self.categories or None)}\n"
            "Produce your analysis now, following the claim-tagging rules exactly."
        )

    # -- Deterministic path ---------------------------------------------------
    def _run_deterministic(
        self, pack: EvidencePack, *, fallback_reason: str | None = None, **context: Any
    ) -> AgentOutput:
        statements = self.compose(pack, **context)
        text = "\n".join(s.render() for s in statements)
        return AgentOutput(
            agent=self.name, text=text, statements=statements,
            generated_by="deterministic", fallback_reason=fallback_reason,
        )

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers for deterministic composition
# ---------------------------------------------------------------------------
def _val(pack: EvidencePack, key: str) -> float | None:
    return pack.numeric_index.get(key)


def _fmt(pack: EvidencePack, key: str) -> str | None:
    for item in pack.items:
        if item.key == key:
            return item.formatted()
    return None


def _fact(pack: EvidencePack, key: str, template: str) -> Statement | None:
    """Build a FACT statement from an evidence item, or nothing if absent."""
    formatted = _fmt(pack, key)
    if formatted is None:
        return None
    source = next((i.source for i in pack.items if i.key == key), "computed")
    return Statement(Claim.FACT, template.format(value=formatted), [source])


# ---------------------------------------------------------------------------
# Fundamental Analyst
# ---------------------------------------------------------------------------
class FundamentalAnalyst(BaseAgent):
    name = "fundamental_analyst"
    system_prompt = prompts.FUNDAMENTAL_ANALYST
    categories = ("scores", "fundamentals", "peer_comparison")

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        out: list[Statement] = []
        add = lambda s: out.append(s) if s else None  # noqa: E731

        period = _fmt(pack, "reporting_period")
        if period:
            add(Statement(
                Claim.FACT,
                f"The most recent reporting period on file is {period}.",
                [next((i.source for i in pack.items if i.key == "reporting_period"), "")],
            ))

        # --- Growth ---
        add(_fact(pack, "revenue_growth", "Revenue changed {value} year over year."))
        add(_fact(pack, "net_income_growth", "Net income changed {value} year over year."))
        add(_fact(pack, "revenue_cagr", "Revenue compounded at {value} over the multi-year window."))

        # --- Profitability ---
        add(_fact(pack, "net_margin", "Net margin is {value}."))
        add(_fact(pack, "operating_margin", "Operating margin is {value}."))
        add(_fact(pack, "roe", "Return on equity is {value}."))
        add(_fact(pack, "roic", "Return on invested capital is {value}."))

        roic = _val(pack, "roic")
        if roic is not None:
            if roic > 0.15:
                add(Statement(
                    Claim.INFERENCE,
                    f"ROIC of {_fmt(pack, 'roic')} is comfortably above a plausible cost "
                    "of capital, which is consistent with genuine value creation rather "
                    "than balance-sheet growth alone.",
                    ["computed:roic"],
                ))
            elif roic < 0.05:
                add(Statement(
                    Claim.INFERENCE,
                    f"ROIC of {_fmt(pack, 'roic')} is likely at or below the cost of "
                    "capital, meaning incremental invested capital may not be creating value.",
                    ["computed:roic"],
                ))

        # --- Balance sheet ---
        add(_fact(pack, "debt_to_equity", "Debt to equity stands at {value}."))
        add(_fact(pack, "net_debt_to_ebitda", "Net debt to EBITDA is {value}."))
        add(_fact(pack, "interest_coverage", "Interest coverage is {value}."))
        nde = _val(pack, "net_debt_to_ebitda")
        if nde is not None:
            if nde > 3.0:
                add(Statement(
                    Claim.INFERENCE,
                    f"Net debt of {_fmt(pack, 'net_debt_to_ebitda')} EBITDA is a "
                    "meaningful constraint: it limits flexibility and raises sensitivity "
                    "to any earnings shortfall or rate move.",
                    ["computed:net_debt_to_ebitda"],
                ))
            elif nde < 1.0:
                add(Statement(
                    Claim.INFERENCE,
                    "Leverage is low, which reduces financial risk and preserves "
                    "capacity to act on opportunities.",
                    ["computed:net_debt_to_ebitda"],
                ))

        # --- Cash flow ---
        add(_fact(pack, "fcf_margin", "Free cash flow margin is {value}."))
        add(_fact(pack, "cash_conversion", "Cash conversion (OCF/net income) is {value}."))
        conversion = _val(pack, "cash_conversion")
        if conversion is not None and conversion < 0.8:
            add(Statement(
                Claim.INFERENCE,
                f"Cash conversion of {_fmt(pack, 'cash_conversion')} means reported "
                "profit is not fully arriving as cash. This warrants scrutiny of "
                "working capital and receivables quality.",
                ["computed:cash_conversion"],
            ))

        # --- Valuation, including peer comparison ---
        add(_fact(pack, "pe", "The shares trade on a P/E of {value}."))
        add(_fact(pack, "ev_ebitda", "EV/EBITDA is {value}."))
        add(_fact(pack, "pb", "P/B is {value}."))
        add(_fact(pack, "fcf_yield", "Free cash flow yield is {value}."))

        pe, sector_pe = _val(pack, "pe"), _val(pack, "sector_median_pe")
        if pe is not None and sector_pe:
            discount = (pe - sector_pe) / sector_pe
            add(Statement(
                Claim.CALCULATION,
                f"P/E of {_fmt(pack, 'pe')} against a sector median of "
                f"{_fmt(pack, 'sector_median_pe')} is a "
                f"{abs(discount):.0%} {'discount' if discount < 0 else 'premium'}.",
                ["computed:sector peers"],
            ))
            add(Statement(
                Claim.INFERENCE,
                "The shares appear inexpensive relative to sector peers, though a "
                "discount can reflect a real problem rather than an opportunity."
                if discount < -0.1 else
                "The shares are not cheap relative to sector peers, so the case must "
                "rest on growth or quality rather than valuation."
                if discount > 0.1 else
                "The shares trade broadly in line with sector peers on earnings.",
                ["computed:sector peers"],
            ))

        # --- Score, and what is missing ---
        score = _fmt(pack, "fundamental_score")
        if score:
            add(Statement(
                Claim.CALCULATION,
                f"The computed fundamental score is {score}, derived from valuation, "
                "quality, growth, profitability, balance sheet and cash flow components.",
                ["computed:fundamental_score"],
            ))
        for unknown in sorted(set(pack.unknowns))[:6]:
            add(Statement(Claim.UNKNOWN, f"{unknown} could not be determined from the available data."))
        return out


# ---------------------------------------------------------------------------
# Technical Analyst
# ---------------------------------------------------------------------------
class TechnicalAnalyst(BaseAgent):
    name = "technical_analyst"
    system_prompt = prompts.TECHNICAL_ANALYST
    categories = ("scores", "technicals")

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        out: list[Statement] = []
        add = lambda s: out.append(s) if s else None  # noqa: E731

        trend = _fmt(pack, "trend")
        price = _val(pack, "price")
        if trend:
            add(Statement(
                Claim.FACT, f"The trend classification is {trend}.", ["computed:technical"]
            ))
        add(_fact(pack, "price", "The last price is {value}."))
        add(_fact(pack, "sma50", "The 50-day SMA is {value}."))
        add(_fact(pack, "sma200", "The 200-day SMA is {value}."))

        sma200 = _val(pack, "sma200")
        if price is not None and sma200:
            gap = (price - sma200) / sma200
            add(Statement(
                Claim.CALCULATION,
                f"Price is {abs(gap):.1%} {'above' if gap > 0 else 'below'} the 200-day SMA.",
                ["computed:technical"],
            ))

        add(_fact(pack, "rsi14", "RSI(14) reads {value}."))
        rsi = _val(pack, "rsi14")
        if rsi is not None:
            if rsi >= 70:
                add(Statement(
                    Claim.INFERENCE,
                    "RSI is in overbought territory, which raises the odds of "
                    "consolidation and argues against chasing an entry here.",
                    ["computed:technical"],
                ))
            elif rsi <= 30:
                add(Statement(
                    Claim.INFERENCE,
                    "RSI is oversold. That can mark exhaustion, but in a downtrend it "
                    "more often marks continuation, so confirmation is required.",
                    ["computed:technical"],
                ))

        add(_fact(pack, "momentum_3m", "The 3-month return is {value}."))
        add(_fact(pack, "momentum_12m", "The 12-month return is {value}."))
        add(_fact(pack, "relative_strength_3m", "The 3-month return versus the benchmark is {value}."))
        rs = _val(pack, "relative_strength_3m")
        if rs is not None:
            add(Statement(
                Claim.INFERENCE,
                "The name is outperforming the benchmark, which is supportive for a "
                "momentum-based entry." if rs > 0.02 else
                "The name is lagging the benchmark, which weakens any momentum case."
                if rs < -0.02 else
                "Performance is broadly in line with the benchmark; relative strength "
                "offers no clear edge either way.",
                ["computed:technical"],
            ))

        add(_fact(pack, "volume_ratio", "Volume is {value} its 20-day average."))
        add(_fact(pack, "volatility_20d", "Annualised 20-day volatility is {value}."))
        add(_fact(pack, "atr_pct", "ATR is {value} of price."))
        add(_fact(pack, "support_levels", "Identified support levels: {value}."))
        add(_fact(pack, "resistance_levels", "Identified resistance levels: {value}."))

        for signal in pack.signals:
            add(Statement(
                Claim.FACT,
                f"Signal detected — {signal['name']} ({signal['direction']}): "
                f"{signal['description']}",
                ["computed:technical"],
            ))

        bullish = sum(1 for s in pack.signals if s["direction"] == "bullish")
        bearish = sum(1 for s in pack.signals if s["direction"] == "bearish")
        if pack.signals:
            if bullish and bearish:
                add(Statement(
                    Claim.INFERENCE,
                    f"The technical picture is mixed: {bullish} bullish and {bearish} "
                    "bearish signals are active simultaneously. Conflicting signals argue "
                    "for a smaller position or waiting for resolution.",
                    ["computed:technical"],
                ))
            elif bullish:
                add(Statement(
                    Claim.INFERENCE,
                    "The active signals are uniformly constructive, with no bearish "
                    "signal currently offsetting them.",
                    ["computed:technical"],
                ))
            elif bearish:
                add(Statement(
                    Claim.INFERENCE,
                    "The active signals are uniformly negative. A long entry would be "
                    "fighting the current technical evidence.",
                    ["computed:technical"],
                ))

        score = _fmt(pack, "technical_score")
        if score:
            add(Statement(
                Claim.CALCULATION, f"The computed technical score is {score}.",
                ["computed:technical_score"],
            ))
        return out


# ---------------------------------------------------------------------------
# Event Analyst
# ---------------------------------------------------------------------------
class EventAnalyst(BaseAgent):
    name = "event_analyst"
    system_prompt = prompts.EVENT_ANALYST
    categories = ("scores", "sentiment")

    def build_prompt(self, pack: EvidencePack, **context: Any) -> str:
        return (
            "EVIDENCE\n========\n"
            f"{pack.render(categories=('scores', 'sentiment'))}\n"
            "Analyse the corporate events and disclosures above. If none are present, "
            "say so explicitly rather than speculating about what might exist."
        )

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        out: list[Statement] = []
        if not pack.events:
            out.append(Statement(
                Claim.UNKNOWN,
                "No corporate disclosures or news items are stored for this company "
                "within the lookback window. This is an absence of data, not evidence "
                "that nothing has happened — EGX disclosure coverage in this system "
                "depends on the configured provider.",
            ))
            return out

        by_kind: dict[str, int] = {}
        for event in pack.events:
            kind = event.get("kind", "OTHER")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            out.append(Statement(
                Claim.FACT,
                f"[{event.get('date') or 'undated'}] {kind}: {event.get('title')}",
                [event.get("source", "unknown")],
            ))

        material = {"M&A", "EARNINGS", "BUYBACK", "CONTRACT", "CAPITAL_ACTION", "DIVIDEND"}
        significant = [k for k in by_kind if k in material]
        if significant:
            out.append(Statement(
                Claim.INFERENCE,
                f"The most material disclosure categories present are "
                f"{', '.join(sorted(significant))}. These are the events with a "
                "plausible direct effect on valuation or capital structure.",
                ["computed:event classification"],
            ))
        else:
            out.append(Statement(
                Claim.INFERENCE,
                "The disclosures on file are routine or administrative. None obviously "
                "changes the investment case.",
                ["computed:event classification"],
            ))

        score = _fmt(pack, "catalyst_score")
        if score:
            out.append(Statement(
                Claim.CALCULATION,
                f"The computed catalyst score is {score}, weighting event type, "
                "importance and recency.",
                ["computed:catalyst_score"],
            ))
        sentiment = _val(pack, "news_sentiment")
        if sentiment is not None:
            out.append(Statement(
                Claim.CALCULATION,
                f"Lexicon sentiment over recent news is {sentiment:+.2f} on a -1 to +1 "
                "scale. This is a keyword heuristic, not an assessment of the company.",
                ["computed:keyword lexicon"],
            ))
        out.append(Statement(
            Claim.UNKNOWN,
            "Forward-looking scheduled events (earnings dates, assembly meetings) are "
            "not present in the evidence and have not been assumed.",
        ))
        return out


# ---------------------------------------------------------------------------
# Bear Analyst
# ---------------------------------------------------------------------------
class BearAnalyst(BaseAgent):
    name = "bear_analyst"
    system_prompt = prompts.BEAR_ANALYST
    categories = ("scores", "fundamentals", "technicals", "peer_comparison", "quant_factors")

    def build_prompt(self, pack: EvidencePack, **context: Any) -> str:
        bull_case = context.get("bull_case") or "(no bull case supplied)"
        direction = context.get("direction", "LONG")
        return (
            "EVIDENCE\n========\n"
            f"{pack.render()}\n"
            f"PROPOSED POSITION: {direction}\n\n"
            "THESIS UNDER ATTACK\n===================\n"
            f"{bull_case}\n\n"
            "Attack this thesis using only the evidence above. Answer every question "
            "in your instructions. Be specific and quantitative."
        )

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        out: list[Statement] = []
        add = lambda s: out.append(s) if s else None  # noqa: E731

        found_concern = False

        # --- Valuation risk ---
        pe, sector_pe = _val(pack, "pe"), _val(pack, "sector_median_pe")
        if pe is not None and pe > 20:
            found_concern = True
            add(Statement(
                Claim.INFERENCE,
                f"At a P/E of {_fmt(pack, 'pe')} the shares already discount continued "
                "execution. Any disappointment is likely to de-rate the multiple as well "
                "as cut earnings — the two compound.",
                ["computed:pe"],
            ))
        if pe is not None and sector_pe and pe < sector_pe * 0.7:
            found_concern = True
            add(Statement(
                Claim.OPINION,
                f"The discount to the sector median ({_fmt(pack, 'pe')} versus "
                f"{_fmt(pack, 'sector_median_pe')}) should be treated as a question, not "
                "an answer. Persistent discounts usually reflect governance, liquidity or "
                "earnings-quality concerns the multiple is correctly pricing.",
                ["computed:sector peers"],
            ))

        # --- Earnings quality ---
        conversion = _val(pack, "cash_conversion")
        if conversion is not None and conversion < 0.9:
            found_concern = True
            add(Statement(
                Claim.FACT,
                f"Cash conversion is {_fmt(pack, 'cash_conversion')}, below 1.0x.",
                ["computed:cash_conversion"],
            ))
            add(Statement(
                Claim.INFERENCE,
                "Profit is not fully converting to cash. That is the single most common "
                "precursor to a negative earnings surprise, and it directly contradicts "
                "any thesis resting on reported profitability.",
                ["computed:cash_conversion"],
            ))

        # --- Leverage ---
        nde = _val(pack, "net_debt_to_ebitda")
        coverage = _val(pack, "interest_coverage")
        if nde is not None and nde > 2.5:
            found_concern = True
            add(Statement(
                Claim.FACT, f"Net debt to EBITDA is {_fmt(pack, 'net_debt_to_ebitda')}.",
                ["computed:net_debt_to_ebitda"],
            ))
            add(Statement(
                Claim.INFERENCE,
                "This leverage means a moderate EBITDA decline produces a much larger "
                "move in equity value. In an Egyptian rate environment, refinancing risk "
                "is a live concern rather than a theoretical one.",
                ["computed:net_debt_to_ebitda"],
            ))
        if coverage is not None and coverage < 3.0:
            found_concern = True
            add(Statement(
                Claim.INFERENCE,
                f"Interest coverage of {_fmt(pack, 'interest_coverage')} leaves little "
                "margin for an earnings decline before debt service becomes binding.",
                ["computed:interest_coverage"],
            ))

        # --- Growth deterioration ---
        revenue_growth = _val(pack, "revenue_growth")
        cagr = _val(pack, "revenue_cagr")
        if revenue_growth is not None and cagr is not None and revenue_growth < cagr - 0.05:
            found_concern = True
            add(Statement(
                Claim.CALCULATION,
                f"Latest revenue growth of {_fmt(pack, 'revenue_growth')} is running below "
                f"the multi-year CAGR of {_fmt(pack, 'revenue_cagr')}.",
                ["computed:growth"],
            ))
            add(Statement(
                Claim.INFERENCE,
                "Growth is decelerating relative to its own trend. Any valuation resting "
                "on the historical growth rate is resting on a rate the company is no "
                "longer achieving.",
                ["computed:growth"],
            ))
        if revenue_growth is not None and revenue_growth < 0:
            found_concern = True
            add(Statement(
                Claim.FACT, f"Revenue declined {_fmt(pack, 'revenue_growth')} year over year.",
                ["computed:revenue_growth"],
            ))

        # --- Technical breakdown ---
        for signal in pack.signals:
            if signal["direction"] == "bearish":
                found_concern = True
                add(Statement(
                    Claim.FACT,
                    f"Bearish signal active — {signal['name']}: {signal['description']}",
                    ["computed:technical"],
                ))
        price, sma200 = _val(pack, "price"), _val(pack, "sma200")
        if price is not None and sma200 and price < sma200:
            found_concern = True
            add(Statement(
                Claim.INFERENCE,
                "Price is below the 200-day SMA. Buying below the long-term average means "
                "the market's medium-term verdict is currently negative, whatever the "
                "fundamental case says.",
                ["computed:technical"],
            ))

        # --- Volatility / liquidity ---
        volatility = _val(pack, "volatility_20d")
        if volatility is not None and volatility > 0.45:
            found_concern = True
            add(Statement(
                Claim.INFERENCE,
                f"Annualised volatility of {_fmt(pack, 'volatility_20d')} means a "
                "sensible stop sits far from entry, so either the position must be small "
                "or the risk per position is larger than it appears.",
                ["computed:technical"],
            ))

        # --- Evidence quality is itself a bear argument -------------------------
        coverage_score = _val(pack, "score_coverage")
        if coverage_score is not None and coverage_score < 0.8:
            found_concern = True
            add(Statement(
                Claim.INFERENCE,
                f"Only {coverage_score:.0%} of the scoring components had data. The score "
                "is therefore an opinion formed on partial information, and conviction "
                "should be capped accordingly.",
                ["computed:score_coverage"],
            ))
        if pack.unknowns:
            add(Statement(
                Claim.UNKNOWN,
                f"{len(set(pack.unknowns))} metric(s) are unavailable, including "
                f"{'; '.join(sorted(set(pack.unknowns))[:3])}. Each is a gap the bull case "
                "is implicitly assuming away.",
            ))
        for warning in pack.warnings:
            add(Statement(Claim.FACT, f"Data quality warning: {warning}", ["data quality"]))

        # --- Honest reporting of a weak bear case -------------------------------
        if not found_concern:
            add(Statement(
                Claim.OPINION,
                "The available evidence does not support a strong bear case. Leverage, "
                "cash conversion, growth and technical structure are all within "
                "unremarkable ranges. The honest conclusion is that the main risk here is "
                "what the evidence does not cover, rather than anything it reveals.",
            ))
        add(Statement(
            Claim.OPINION,
            "Structural caution for any EGX position: currency volatility, policy shifts "
            "and thin secondary liquidity can dominate company fundamentals over the "
            "holding period. These are not company-specific and are not captured in the "
            "metrics above.",
        ))
        return out


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------
@dataclass
class PortfolioDecision:
    action: str                    # BUY | HOLD | SELL | WATCH
    conviction: float              # 0-10
    rationale: str
    why_now: str = ""
    exit_conditions: list[str] = field(default_factory=list)
    output: AgentOutput | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "conviction": round(self.conviction, 1),
            "rationale": self.rationale,
            "why_now": self.why_now,
            "exit_conditions": self.exit_conditions,
            "output": self.output.to_dict() if self.output else None,
        }


class PortfolioManager(BaseAgent):
    name = "portfolio_manager"
    system_prompt = prompts.PORTFOLIO_MANAGER

    def build_prompt(self, pack: EvidencePack, **context: Any) -> str:
        sections = ["EVIDENCE\n========", pack.render()]
        for label, key in (
            ("FUNDAMENTAL ANALYSIS", "fundamental"),
            ("TECHNICAL ANALYSIS", "technical"),
            ("EVENT ANALYSIS", "event"),
            ("BEAR CASE", "bear"),
        ):
            output = context.get(key)
            if isinstance(output, AgentOutput) and output.text:
                sections.append(f"\n{label}\n{'=' * len(label)}\n{output.text}")
        if context.get("portfolio_context"):
            sections.append(f"\nPORTFOLIO CONTEXT\n=================\n{context['portfolio_context']}")
        sections.append(
            "\nMake the decision now. Your first line must be the decision word alone: "
            "BUY, HOLD, SELL or WATCH."
        )
        return "\n".join(sections)

    def decide(self, pack: EvidencePack, **context: Any) -> PortfolioDecision:
        output = self.run(pack, **context)
        action, conviction = self._derive_action(pack, output, **context)
        return PortfolioDecision(
            action=action,
            conviction=conviction,
            rationale=output.text,
            why_now=self._why_now(pack, action),
            exit_conditions=self._exit_conditions(pack),
            output=output,
        )

    @staticmethod
    def score_based_action(
        pack: EvidencePack, *, held: bool = False
    ) -> tuple[str, float]:
        """Map the computed score and evidence quality onto an action.

        This is the deterministic core of the decision. It is shared by the
        LLM and deterministic paths so the two can never disagree about what
        the numbers imply.
        """
        alpha = _val(pack, "alpha_score")
        coverage = _val(pack, "score_coverage") or 0.0
        if alpha is None:
            return ("WATCH", 0.0)

        # Conviction scales with score strength and is capped by evidence coverage.
        conviction = max(0.0, min(10.0, (alpha - 40.0) / 5.0))
        conviction *= max(0.4, min(1.0, coverage / 0.9))
        bearish = sum(1 for s in pack.signals if s["direction"] == "bearish")
        bullish = sum(1 for s in pack.signals if s["direction"] == "bullish")
        if bearish > bullish:
            conviction *= 0.85
        if pack.contains_synthetic:
            conviction = min(conviction, 1.0)   # never a real call on fake data

        if alpha >= 70 and coverage >= 0.7:
            action = "BUY"
        elif alpha >= 58 and coverage >= 0.6:
            action = "BUY" if not held else "HOLD"
        elif alpha >= 35:
            action = "HOLD" if held else "WATCH"
        else:
            action = "SELL" if held else "WATCH"
        return action, round(conviction, 1)

    def _derive_action(
        self, pack: EvidencePack, output: AgentOutput, **context: Any
    ) -> tuple[str, float]:
        """Decide from the computed score and evidence quality.

        The decision is anchored to deterministic inputs even when an LLM wrote
        the rationale. An LLM-stated decision is honoured only when it agrees
        with a defensible band — the model argues, the system decides.
        """
        action, conviction = self.score_based_action(
            pack, held=bool(context.get("currently_held"))
        )
        if _val(pack, "alpha_score") is None:
            return action, conviction

        # An LLM decision that lands in an adjacent band is respected.
        stated = self._stated_action(output.text)
        if stated and output.generated_by == "llm":
            neighbours = {
                "BUY": {"BUY", "HOLD"}, "HOLD": {"BUY", "HOLD", "WATCH"},
                "WATCH": {"HOLD", "WATCH", "SELL"}, "SELL": {"WATCH", "SELL"},
            }
            if stated in neighbours.get(action, set()):
                action = stated

        return action, round(conviction, 1)

    def compose(self, pack: EvidencePack, **context: Any) -> list[Statement]:
        """Deterministic decision memo, composed from the computed evidence."""
        held = bool(context.get("currently_held"))
        action, conviction = self.score_based_action(pack, held=held)
        out: list[Statement] = []

        alpha = _val(pack, "alpha_score")
        coverage = _val(pack, "score_coverage")

        out.append(Statement(
            Claim.CALCULATION,
            f"DECISION: {action}. The GMG score is "
            f"{_fmt(pack, 'alpha_score') or 'UNAVAILABLE'}"
            + (f", computed on {coverage:.0%} of component weight" if coverage else "")
            + f", and the position is {'currently held' if held else 'not currently held'}.",
            ["computed:master_score"],
        ))

        # --- What the sub-scores contribute -------------------------------
        for key, label in (
            ("fundamental_score", "Fundamental"), ("technical_score", "Technical"),
            ("quantitative_score", "Quantitative"), ("catalyst_score", "Catalyst"),
            ("risk_score", "Risk (higher = lower risk)"),
        ):
            formatted = _fmt(pack, key)
            if formatted:
                out.append(Statement(
                    Claim.FACT, f"{label} score: {formatted}.", ["computed:master_score"]
                ))

        # --- Reasoning ------------------------------------------------------
        if alpha is not None:
            if action == "BUY":
                out.append(Statement(
                    Claim.INFERENCE,
                    "The composite evidence is strong enough, and complete enough, to "
                    "justify committing capital rather than continuing to observe.",
                    ["computed:master_score"],
                ))
            elif action == "HOLD":
                out.append(Statement(
                    Claim.INFERENCE,
                    "The evidence supports continuing to hold but does not support "
                    "adding. The case is intact rather than improving.",
                    ["computed:master_score"],
                ))
            elif action == "WATCH":
                out.append(Statement(
                    Claim.INFERENCE,
                    "The evidence is not strong enough to commit capital. Watching costs "
                    "nothing; a position taken on an inconclusive case does not.",
                    ["computed:master_score"],
                ))
            else:
                out.append(Statement(
                    Claim.INFERENCE,
                    "The composite evidence has deteriorated below the level that "
                    "justifies holding the position.",
                    ["computed:master_score"],
                ))

        # --- Engage with the bear case, rather than ignoring it -------------
        bear = context.get("bear")
        if isinstance(bear, AgentOutput) and bear.statements:
            concerns = [
                s for s in bear.statements
                if s.claim in (Claim.INFERENCE, Claim.FACT)
            ]
            if concerns:
                out.append(Statement(
                    Claim.INFERENCE,
                    "The bear case raises "
                    f"{len(concerns)} evidence-backed concern(s), the first being: "
                    f"{concerns[0].text}",
                    ["bear_analyst"],
                ))
            unknowns = [s for s in bear.statements if s.claim is Claim.UNKNOWN]
            if unknowns:
                out.append(Statement(
                    Claim.INFERENCE,
                    "Gaps identified by the bear analyst reduce how much weight this "
                    "decision can carry, and are reflected in the conviction below.",
                    ["bear_analyst"],
                ))

        # --- Conviction, and why it is capped -------------------------------
        reasons: list[str] = []
        if coverage is not None and coverage < 0.9:
            reasons.append(f"evidence covers only {coverage:.0%} of scoring components")
        bearish = sum(1 for s in pack.signals if s["direction"] == "bearish")
        bullish = sum(1 for s in pack.signals if s["direction"] == "bullish")
        if bearish > bullish:
            reasons.append(f"{bearish} bearish signal(s) outnumber {bullish} bullish")
        if pack.contains_synthetic:
            reasons.append("the underlying data is synthetic and cannot support a real call")
        out.append(Statement(
            Claim.CALCULATION,
            f"CONVICTION: {conviction}/10, scaled from the composite score"
            + (f" and reduced because {'; '.join(reasons)}" if reasons else "")
            + ".",
            ["computed:master_score"],
        ))

        # --- Timing and exits -----------------------------------------------
        out.append(Statement(Claim.INFERENCE, f"WHY NOW — {self._why_now(pack, action)}"))
        for condition in self._exit_conditions(pack):
            out.append(Statement(Claim.INFERENCE, f"EXIT CONDITION — {condition}"))

        for warning in pack.warnings:
            out.append(Statement(Claim.FACT, f"Data quality warning: {warning}", ["data quality"]))
        return out

    @staticmethod
    def _stated_action(text: str) -> str | None:
        for line in (text or "").splitlines():
            stripped = line.strip().upper().lstrip("#* ").strip()
            for word in ("BUY", "SELL", "HOLD", "WATCH"):
                if stripped == word or stripped.startswith(f"{word} ") or stripped.startswith(f"DECISION: {word}"):
                    return word
        return None

    def _why_now(self, pack: EvidencePack, action: str) -> str:
        triggers = [
            s["description"] for s in pack.signals
            if s["direction"] == ("bullish" if action == "BUY" else "bearish")
        ]
        recent_events = [e["title"] for e in pack.events[:2]]
        if triggers or recent_events:
            parts = triggers[:2] + recent_events
            return "Timing rests on: " + "; ".join(parts)
        return (
            "No specific timing trigger is present in the evidence. The case rests on "
            "valuation and fundamentals rather than an imminent catalyst."
        )

    def _exit_conditions(self, pack: EvidencePack) -> list[str]:
        conditions: list[str] = []
        support = _fmt(pack, "support_levels")
        if support:
            conditions.append(f"A decisive close below support ({support}).")
        if _val(pack, "sma200") is not None:
            conditions.append(
                f"Price closing below the 200-day SMA ({_fmt(pack, 'sma200')}) and "
                "failing to reclaim it."
            )
        if _val(pack, "cash_conversion") is not None:
            conditions.append(
                "Cash conversion deteriorating further below 1.0x in the next reported period."
            )
        if _val(pack, "net_debt_to_ebitda") is not None:
            conditions.append("Net debt to EBITDA rising materially above the current level.")
        fundamental_score = _fmt(pack, "fundamental_score")
        if fundamental_score:
            conditions.append(
                f"A material fall in the fundamental score from its current "
                f"{fundamental_score} without a corresponding fall in price."
            )
        else:
            conditions.append(
                "A material deterioration in the fundamental score without a "
                "corresponding fall in price."
            )
        return conditions


def all_agents(client: LlmClient | None = None) -> dict[str, BaseAgent]:
    client = client or LlmClient()
    return {
        "fundamental": FundamentalAnalyst(client),
        "technical": TechnicalAnalyst(client),
        "event": EventAnalyst(client),
        "bear": BearAnalyst(client),
        "portfolio_manager": PortfolioManager(client),
    }
