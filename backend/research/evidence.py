"""Evidence packs — the anti-hallucination boundary.

An agent never sees the open internet, never recalls a company from training,
and never receives a free-form prompt about a stock. It receives an *evidence
pack*: a closed set of facts assembled from the database, each with its source
and retrieval time, plus an explicit list of what is **not** known.

Two mechanisms make this binding:

:class:`EvidencePack.numeric_index`
    Every number in the pack, indexed. Agent output is checked against it, and a
    figure that does not appear here is a fabrication by definition.

:class:`EvidencePack.unknowns`
    What could not be determined. Naming absences explicitly is what lets an
    agent say "no reliable forward estimate was available" instead of inventing
    one to fill the gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from backend.analytics.service import StockAnalysis
from backend.core.data_quality import is_available


@dataclass
class EvidenceItem:
    """One verifiable fact available to an agent."""

    key: str
    label: str
    value: Any
    unit: str = ""
    source: str = "UNKNOWN"
    period: str | None = None
    category: str = "general"

    @property
    def numeric(self) -> float | None:
        return float(self.value) if isinstance(self.value, (int, float)) else None

    def render(self) -> str:
        return f"{self.label}: {self.formatted()}  [source: {self.source}" + (
            f", period: {self.period}]" if self.period else "]"
        )

    def formatted(self) -> str:
        if not is_available(self.value):
            return "UNAVAILABLE"
        if isinstance(self.value, float):
            if self.unit == "percent":
                return f"{self.value:.2%}"
            if self.unit == "times":
                return f"{self.value:.2f}x"
            if self.unit == "currency":
                return f"{self.value:,.0f}"
            if self.unit == "score":
                return f"{self.value:.1f}/100"
            return f"{self.value:,.4f}".rstrip("0").rstrip(".")
        return str(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label,
            "value": self.value if is_available(self.value) else None,
            "formatted": self.formatted(), "unit": self.unit,
            "source": self.source, "period": self.period, "category": self.category,
        }


@dataclass
class EvidencePack:
    """The complete, closed set of information an agent may reason over."""

    ticker: str
    company_name: str
    sector: str | None
    as_of: date
    items: list[EvidenceItem] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    contains_synthetic: bool = False

    def add(
        self, key: str, label: str, value: Any, *,
        unit: str = "", source: str = "computed", period: str | None = None,
        category: str = "general",
    ) -> None:
        """Record a fact, or record its absence. Never silently drop either."""
        if not is_available(value):
            self.unknowns.append(label)
            return
        self.items.append(
            EvidenceItem(key, label, value, unit, source, period, category)
        )
        if source and source not in self.sources:
            self.sources.append(source)

    def by_category(self, category: str) -> list[EvidenceItem]:
        return [i for i in self.items if i.category == category]

    @property
    def numeric_index(self) -> dict[str, float]:
        """Every numeric fact, keyed. The validator checks agent output against this."""
        return {i.key: i.numeric for i in self.items if i.numeric is not None}

    def numeric_values(self) -> list[float]:
        return [v for v in self.numeric_index.values() if v is not None]

    def render(self, *, categories: Iterable[str] | None = None) -> str:
        """Format the pack for an LLM prompt."""
        wanted = set(categories) if categories else None
        lines = [
            f"COMPANY: {self.company_name} ({self.ticker})",
            f"SECTOR: {self.sector or 'UNKNOWN'}",
            f"AS OF: {self.as_of.isoformat()}",
            "",
        ]
        if self.contains_synthetic:
            lines += [
                "!! WARNING: THIS EVIDENCE CONTAINS SYNTHETIC DEMONSTRATION DATA.",
                "!! The figures below are fictional and describe no real security.",
                "",
            ]

        grouped: dict[str, list[EvidenceItem]] = {}
        for item in self.items:
            if wanted and item.category not in wanted:
                continue
            grouped.setdefault(item.category, []).append(item)

        for category, items in grouped.items():
            lines.append(f"--- {category.upper().replace('_', ' ')} ---")
            lines.extend(f"  {i.render()}" for i in items)
            lines.append("")

        if self.signals:
            lines.append("--- DETECTED TECHNICAL SIGNALS ---")
            for signal in self.signals:
                lines.append(
                    f"  {signal['name']} ({signal['direction']}, strength "
                    f"{signal['strength']}): {signal['description']}"
                )
            lines.append("")

        if self.events:
            lines.append("--- CORPORATE EVENTS AND DISCLOSURES ---")
            for event in self.events:
                lines.append(
                    f"  [{event.get('date') or 'undated'}] {event.get('kind')}: "
                    f"{event.get('title')}  [source: {event.get('source')}]"
                )
            lines.append("")

        if self.unknowns:
            lines.append("--- NOT AVAILABLE (you MUST NOT estimate these) ---")
            lines.extend(f"  {u}" for u in sorted(set(self.unknowns)))
            lines.append("")

        if self.warnings:
            lines.append("--- DATA QUALITY WARNINGS ---")
            lines.extend(f"  {w}" for w in self.warnings)
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "sector": self.sector,
            "as_of": self.as_of.isoformat(),
            "items": [i.to_dict() for i in self.items],
            "unknowns": sorted(set(self.unknowns)),
            "events": self.events,
            "signals": self.signals,
            "warnings": self.warnings,
            "sources": self.sources,
            "contains_synthetic": self.contains_synthetic,
        }


# ---------------------------------------------------------------------------
# Pack construction
# ---------------------------------------------------------------------------
_FUNDAMENTAL_KEYS: tuple[tuple[str, str, str], ...] = (
    ("revenue_growth", "Revenue growth (YoY)", "percent"),
    ("revenue_cagr", "Revenue CAGR", "percent"),
    ("ebitda_growth", "EBITDA growth (YoY)", "percent"),
    ("net_income_growth", "Net income growth (YoY)", "percent"),
    ("eps_growth", "EPS growth (YoY)", "percent"),
    ("fcf_growth", "Free cash flow growth (YoY)", "percent"),
    ("gross_margin", "Gross margin", "percent"),
    ("ebitda_margin", "EBITDA margin", "percent"),
    ("operating_margin", "Operating margin", "percent"),
    ("net_margin", "Net margin", "percent"),
    ("roe", "Return on equity", "percent"),
    ("roa", "Return on assets", "percent"),
    ("roic", "Return on invested capital", "percent"),
    ("debt_to_equity", "Debt to equity", "times"),
    ("net_debt_to_ebitda", "Net debt to EBITDA", "times"),
    ("current_ratio", "Current ratio", "times"),
    ("interest_coverage", "Interest coverage", "times"),
    ("free_cash_flow", "Free cash flow", "currency"),
    ("fcf_margin", "Free cash flow margin", "percent"),
    ("cash_conversion", "Cash conversion (OCF/net income)", "times"),
    ("market_cap", "Market capitalisation", "currency"),
    ("pe", "P/E", "times"),
    ("pb", "P/B", "times"),
    ("ps", "P/S", "times"),
    ("ev_ebitda", "EV/EBITDA", "times"),
    ("ev_sales", "EV/Sales", "times"),
    ("fcf_yield", "Free cash flow yield", "percent"),
    ("dividend_yield", "Dividend yield", "percent"),
)

_TECHNICAL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("price", "Last price", "currency"),
    ("sma20", "SMA 20", "currency"),
    ("sma50", "SMA 50", "currency"),
    ("sma100", "SMA 100", "currency"),
    ("sma200", "SMA 200", "currency"),
    ("ema20", "EMA 20", "currency"),
    ("ema50", "EMA 50", "currency"),
    ("rsi14", "RSI (14)", ""),
    ("macd", "MACD line", ""),
    ("macd_signal", "MACD signal line", ""),
    ("atr_pct", "ATR as % of price", "percent"),
    ("bb_position", "Position within Bollinger bands (0=lower, 1=upper)", ""),
    ("volume_ratio", "Volume vs 20-day average", "times"),
    ("volatility_20d", "Annualised volatility (20d)", "percent"),
    ("momentum_1m", "1-month return", "percent"),
    ("momentum_3m", "3-month return", "percent"),
    ("momentum_6m", "6-month return", "percent"),
    ("momentum_12m", "12-month return", "percent"),
    ("relative_strength_3m", "3-month return vs benchmark", "percent"),
)


def build_evidence_pack(analysis: StockAnalysis) -> EvidencePack:
    """Assemble the evidence pack for one stock from its computed analysis."""
    company = analysis.company
    pack = EvidencePack(
        ticker=analysis.ticker,
        company_name=company.name if company else analysis.ticker,
        sector=company.sector if company else None,
        as_of=analysis.as_of,
    )

    # --- Scores (computed, never agent-produced) ----------------------------
    alpha = analysis.alpha
    pack.add("alpha_score", "GMG score", alpha.value, unit="score",
             source="computed:master_score", category="scores")
    for label, sub in (
        ("Fundamental score", alpha.fundamental), ("Technical score", alpha.technical),
        ("Quantitative score", alpha.quant), ("Catalyst score", alpha.catalyst),
        ("Risk score (higher = lower risk)", alpha.risk),
    ):
        pack.add(
            label.split()[0].lower() + "_score", label,
            sub.value if sub else None, unit="score",
            source="computed:master_score", category="scores",
        )
    pack.add("score_confidence", "Score confidence", alpha.score.confidence.value,
             source="computed", category="scores")
    pack.add("score_coverage", "Share of score components with data",
             alpha.score.coverage, unit="percent", source="computed", category="scores")

    # --- Fundamentals --------------------------------------------------------
    fundamental = analysis.fundamental
    if fundamental and not fundamental.insufficient_data:
        for key, label, unit in _FUNDAMENTAL_KEYS:
            metric = fundamental.metrics.get(key)
            pack.add(
                key, label,
                metric.value if metric and metric.available else None,
                unit=unit, source=fundamental.source,
                period=fundamental.latest_period, category="fundamentals",
            )
        pack.add("reporting_period", "Latest reporting period",
                 fundamental.latest_period, source=fundamental.source,
                 category="fundamentals")
        if fundamental.period_end:
            pack.add("period_end", "Period end date", fundamental.period_end.isoformat(),
                     source=fundamental.source, category="fundamentals")
        for key, context in fundamental.peer_context.items():
            pack.add(
                f"sector_median_{key}", f"Sector median {key.upper()}",
                context.get("median"), unit="times",
                source=f"computed:sector peers (n={context.get('count')})",
                category="peer_comparison",
            )
    else:
        pack.unknowns.append(
            "All fundamental metrics — no financial statements are stored for this company"
        )

    # --- Technicals ----------------------------------------------------------
    technical = analysis.technical
    if technical and not technical.insufficient_data:
        for key, label, unit in _TECHNICAL_FIELDS:
            pack.add(key, label, getattr(technical, key, None), unit=unit,
                     source=analysis.price_series.source or "market data",
                     category="technicals")
        pack.add("trend", "Trend classification", technical.trend,
                 source="computed:technical", category="technicals")
        # Levels are stored twice: once as a display string, and once per level
        # as a numeric item so each is individually traceable by the validator.
        if technical.support_levels:
            pack.add("support_levels", "Support levels",
                     ", ".join(f"{v:,.2f}" for v in technical.support_levels),
                     source="computed:technical", category="technicals")
            for i, level in enumerate(technical.support_levels, start=1):
                pack.add(f"support_{i}", f"Support level {i}", level, unit="currency",
                         source="computed:technical", category="technicals")
        if technical.resistance_levels:
            pack.add("resistance_levels", "Resistance levels",
                     ", ".join(f"{v:,.2f}" for v in technical.resistance_levels),
                     source="computed:technical", category="technicals")
            for i, level in enumerate(technical.resistance_levels, start=1):
                pack.add(f"resistance_{i}", f"Resistance level {i}", level, unit="currency",
                         source="computed:technical", category="technicals")
        pack.signals = [s.to_dict() for s in technical.signals]
    else:
        pack.unknowns.append(
            "All technical indicators — insufficient price history "
            f"({len(analysis.price_series)} bars stored)"
        )

    # --- Quant factors -------------------------------------------------------
    if analysis.quant:
        for name, exposure in analysis.quant.factors.items():
            pack.add(f"factor_{name}", f"{name.title()} factor (vs EGX universe)",
                     exposure.score, unit="score",
                     source=f"computed:cross-sectional (n={analysis.quant.universe_size})",
                     category="quant_factors")

    # --- Events --------------------------------------------------------------
    pack.events = [e.to_dict() for e in alpha.catalysts]
    for event in alpha.catalysts:
        if event.source and event.source not in pack.sources:
            pack.sources.append(event.source)

    # --- Sentiment -----------------------------------------------------------
    pack.add("news_sentiment", "Lexicon sentiment over recent news (-1 to +1)",
             alpha.sentiment_value, source="computed:keyword lexicon",
             category="sentiment")

    # --- Warnings / provenance ----------------------------------------------
    pack.warnings = list(alpha.warnings)
    if analysis.price_series.source:
        pack.sources.append(analysis.price_series.source)
    pack.contains_synthetic = any(
        "SYNTHETIC" in (s or "").upper() for s in pack.sources
    )
    if pack.contains_synthetic:
        pack.warnings.insert(
            0,
            "SYNTHETIC DEMONSTRATION DATA — every figure here is fictional and "
            "corresponds to no real security.",
        )
    pack.sources = sorted(set(s for s in pack.sources if s))
    return pack


# ---------------------------------------------------------------------------
# Numeric validation of agent output
# ---------------------------------------------------------------------------
#: Dates would otherwise tokenise into spurious numbers ("2026-08-24" yielding
#: -24), so they are stripped before number extraction.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}")

#: Indicator parameters name a setting, not a claimed value: the 14 in "RSI(14)"
#: and the 20 in "SMA 20" are labels. Stripped so they are not mistaken for data.
_INDICATOR_PARAM_RE = re.compile(
    r"\b(?:RSI|SMA|EMA|ATR|MACD|BB|MA)\s*\(?\s*[\d\s,]+\)?",
    re.IGNORECASE,
)

#: Matches numbers in agent prose, including percentages, multiples and
#: thousands separators.
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")

#: Numbers so common in ordinary prose that flagging them would be noise
#: ("the first of three reasons", "a 50/50 split", years, small counts).
_INNOCUOUS = set(range(0, 13)) | {
    20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100, 200, 500, 1000,
    2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028, 2029, 2030,
}


def extract_numbers(text: str) -> list[float]:
    out: list[float] = []
    cleaned = _INDICATOR_PARAM_RE.sub(" ", _DATE_RE.sub(" ", text or ""))
    for match in _NUMBER_RE.finditer(cleaned):
        try:
            out.append(float(match.group().replace(",", "")))
        except ValueError:
            continue
    return out


def find_unsupported_numbers(
    text: str, pack: EvidencePack, *, tolerance: float = 0.02,
    strict_claims: tuple[str, ...] = ("FACT",),
) -> list[float]:
    """Return numbers in *text* that do not trace to the evidence pack.

    A figure matches if it is within *tolerance* of any evidence value, or of a
    common presentation of one (a ratio written as a percentage, a currency
    figure written in millions or billions). Anything left over is a number the
    agent produced from nowhere.

    Only lines tagged with *strict_claims* are checked. This is deliberate: a
    FACT must restate the evidence, so an untraceable number there is a
    fabrication. A CALCULATION is derived arithmetic whose result will not
    appear in the pack by construction, and verifying arbitrary arithmetic
    would require accepting so many values that the check would catch nothing.
    Derived claims are surfaced separately by :func:`find_derived_numbers`.
    """
    allowed: set[float] = set()

    # Numbers embedded in signal descriptions and event titles are part of the
    # evidence: the pack handed them to the agent, so restating one is not a
    # fabrication.
    narrative = " ".join(
        [s.get("description", "") for s in pack.signals]
        + [str(e.get("title", "")) for e in pack.events]
        + [str(i.value) for i in pack.items if isinstance(i.value, str)]
    )
    for value in extract_numbers(narrative):
        allowed.add(value)
        allowed.add(round(value, 2))

    for value in pack.numeric_values():
        allowed.add(value)
        allowed.add(round(value, 2))
        allowed.add(value * 100.0)          # ratio rendered as a percentage
        allowed.add(round(value * 100.0, 1))
        allowed.add(round(value * 100.0, 2))
        for divisor in (1e3, 1e6, 1e9):     # currency rendered in k/m/bn
            allowed.add(round(value / divisor, 2))
            allowed.add(round(value / divisor, 1))

    scanned = _lines_for_claims(text, strict_claims)
    unsupported: list[float] = []
    for number in extract_numbers(scanned):
        if abs(number) in _INNOCUOUS or number in _INNOCUOUS:
            continue
        matched = any(
            abs(number - candidate) <= max(tolerance, abs(candidate) * tolerance)
            for candidate in allowed
        )
        if not matched:
            unsupported.append(number)
    return unsupported


def _lines_for_claims(text: str, claims: tuple[str, ...]) -> str:
    """Select lines carrying one of *claims*, plus untagged prose.

    Untagged prose is included because an untagged assertion is a factual claim
    by default — that is exactly the sloppiness the tagging rule exists to stop.
    """
    wanted = {c.upper() for c in claims}
    # Untagged prose counts as a factual assertion only, never as a calculation.
    include_untagged = "FACT" in wanted
    selected: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*• ").strip()
        if not stripped:
            continue
        tag = None
        for candidate in ("FACT", "CALCULATION", "INFERENCE", "OPINION", "UNKNOWN"):
            if stripped.upper().startswith(f"{candidate}:"):
                tag = candidate
                break
        if tag in wanted or (tag is None and include_untagged):
            selected.append(stripped)
    return "\n".join(selected)


def find_derived_numbers(text: str, pack: EvidencePack) -> list[float]:
    """Numbers on CALCULATION lines that do not appear verbatim in the evidence.

    These are not fabrications — they are arithmetic the agent performed — but
    they are unverified, and the UI labels them as such so a reader knows which
    figures were restated and which were computed.
    """
    return find_unsupported_numbers(text, pack, strict_claims=("CALCULATION",))
