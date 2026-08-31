"""Market aggregation: index snapshots, movers, breadth, and search.

Everything here is derived from quotes that already carry their own provenance,
and the derivation is carried through rather than hidden:

* **Index levels.** The official EGX 30 / EGX 70 EWI / EGX 100 EWI levels are
  published by the Egyptian Exchange. This module does not have them unless a
  licensed provider supplies them, so it never prints a made-up index level.
  What it can compute honestly is a *constituent composite* — the weighted
  average move of the index members we hold quotes for — and it labels it as
  exactly that, with the number of constituents covered.
* **Coverage.** Every aggregate reports how many companies it actually saw.
  A breadth reading over 4 of 30 constituents is reported as such and not
  dressed up as the market.
* **Demo contamination.** If any input quote is demo data, the aggregate is
  flagged demo too. Fabricated inputs cannot launder into a clean-looking total.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.data.models import Company, PriceBar
from backend.data.saas_models import Quote
from backend.market.quotes import get_quotes, quote_freshness
from backend.market.status import SessionState, market_state

# ---------------------------------------------------------------------------
# Index definitions
# ---------------------------------------------------------------------------
INDEX_DEFINITIONS: list[dict[str, str]] = [
    {
        "code": "EGX30",
        "name": "EGX 30",
        "column": "in_egx30",
        "weighting": "Free-float market capitalisation",
        "description": "The 30 most active EGX constituents by liquidity and market value.",
    },
    {
        "code": "EGX70",
        "name": "EGX 70 EWI",
        "column": "in_egx70",
        "weighting": "Equal weighted",
        "description": "70 companies outside the EGX 30, equally weighted.",
    },
    {
        "code": "EGX100",
        "name": "EGX 100 EWI",
        "column": "in_egx100",
        "weighting": "Equal weighted",
        "description": "The EGX 30 and EGX 70 constituents combined, equally weighted.",
    },
]

UNOFFICIAL_NOTE = (
    "Constituent composite computed by GMG from member quotes. This is not the "
    "official index level published by the Egyptian Exchange."
)


class IndexLevelProvider(abc.ABC):
    """Slot for official exchange index levels.

    Same rule as quotes: without a source, it returns nothing rather than
    inventing a level.
    """

    name = "official-index"
    is_demo = False

    @abc.abstractmethod
    def get_levels(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]: ...

    def is_available(self) -> bool:
        return False


class UnavailableIndexProvider(IndexLevelProvider):
    """The default. No official index feed is connected."""

    def get_levels(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        return {}


_index_provider: IndexLevelProvider = UnavailableIndexProvider()


def set_index_provider(provider: IndexLevelProvider) -> None:
    """Register a licensed official-index feed once one exists."""
    global _index_provider
    _index_provider = provider


def get_index_provider() -> IndexLevelProvider:
    return _index_provider


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class MoverRow:
    ticker: str
    name: str
    sector: str | None
    price: float | None
    change: float | None
    change_pct: float | None
    volume: float | None
    turnover: float | None
    is_demo: bool
    badge: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class IndexSnapshot:
    code: str
    name: str
    weighting: str
    description: str
    level: float | None = None
    change: float | None = None
    change_pct: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    turnover: float | None = None
    constituents: int = 0
    covered: int = 0
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    is_official: bool = False
    is_demo: bool = False
    basis: str = "No data"
    note: str = ""
    series: list[float] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return self.change_pct is not None

    @property
    def coverage_pct(self) -> float | None:
        if not self.constituents:
            return None
        return self.covered / self.constituents

    def to_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        payload["coverage_pct"] = self.coverage_pct
        return payload


@dataclass
class MarketBreadth:
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    total_volume: float | None = None
    total_turnover: float | None = None
    covered: int = 0
    universe: int = 0
    is_demo: bool = False

    @property
    def counted(self) -> int:
        return self.advancers + self.decliners + self.unchanged

    @property
    def advance_decline_ratio(self) -> float | None:
        if not self.decliners:
            return None
        return self.advancers / self.decliners

    @property
    def advancer_pct(self) -> float | None:
        if not self.counted:
            return None
        return self.advancers / self.counted

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "counted": self.counted,
            "advance_decline_ratio": self.advance_decline_ratio,
            "advancer_pct": self.advancer_pct,
        }


@dataclass
class MarketOverview:
    indices: list[IndexSnapshot]
    gainers: list[MoverRow]
    losers: list[MoverRow]
    most_active: list[MoverRow]
    breadth: MarketBreadth
    session: SessionState
    data_note: str
    is_demo: bool
    quote_badge: str
    universe_size: int
    covered: int
    as_of: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "indices": [i.to_dict() for i in self.indices],
            "gainers": [m.to_dict() for m in self.gainers],
            "losers": [m.to_dict() for m in self.losers],
            "most_active": [m.to_dict() for m in self.most_active],
            "breadth": self.breadth.to_dict(),
            "session": self.session.to_dict(),
            "data_note": self.data_note,
            "is_demo": self.is_demo,
            "quote_badge": self.quote_badge,
            "universe_size": self.universe_size,
            "covered": self.covered,
            "as_of": self.as_of.isoformat() if self.as_of else None,
        }


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------
def active_universe(session: Session) -> list[Company]:
    return list(
        session.execute(
            select(Company).where(Company.status == "ACTIVE").order_by(Company.ticker)
        ).scalars().all()
    )


def _weight_for(company: Company, quote: Quote | None, equal: bool) -> float:
    """Market-cap weight when we hold share counts, equal weight otherwise."""
    if equal:
        return 1.0
    if quote is None or quote.price is None or not company.shares_outstanding:
        return 0.0
    return float(company.shares_outstanding) * float(quote.price)


# ---------------------------------------------------------------------------
# Index composites
# ---------------------------------------------------------------------------
def _composite_series(
    session: Session, tickers: Sequence[str], *, days: int = 60
) -> list[float]:
    """An equal-weighted, rebased composite of constituent closes, for the mini
    chart. Returns [] when there is no stored history — never a drawn line with
    no data behind it."""
    if not tickers:
        return []
    start = date.today() - timedelta(days=days * 2)
    rows = session.execute(
        select(PriceBar.timestamp, PriceBar.ticker, PriceBar.close)
        .where(PriceBar.ticker.in_(list(tickers)), PriceBar.timestamp >= start,
               PriceBar.close.isnot(None))
        .order_by(PriceBar.timestamp)
    ).all()
    if not rows:
        return []

    by_day: dict[date, dict[str, float]] = {}
    for stamp, ticker, close in rows:
        by_day.setdefault(stamp, {})[ticker] = float(close)

    ordered = sorted(by_day)[-days:]
    if len(ordered) < 2:
        return []

    base = by_day[ordered[0]]
    series: list[float] = []
    for day in ordered:
        prices = by_day[day]
        shared = [t for t in prices if t in base and base[t]]
        if not shared:
            continue
        series.append(round(100.0 * sum(prices[t] / base[t] for t in shared) / len(shared), 3))
    return series if len(series) >= 2 else []


def index_snapshot(
    session: Session, definition: dict[str, str], quotes: dict[str, Quote],
    companies: Sequence[Company] | None = None,
) -> IndexSnapshot:
    """Build one index snapshot, official if a feed supplies it, otherwise a
    clearly-labelled constituent composite."""
    column = getattr(Company, definition["column"])
    members = [c for c in (companies if companies is not None else active_universe(session))
               if getattr(c, definition["column"])]
    if companies is None and not members:
        members = list(session.execute(select(Company).where(column.is_(True))).scalars().all())

    snap = IndexSnapshot(
        code=definition["code"], name=definition["name"],
        weighting=definition["weighting"], description=definition["description"],
        constituents=len(members),
    )

    official = get_index_provider()
    if official.is_available():
        level = official.get_levels([definition["code"]]).get(definition["code"])
        if level:
            snap.level = level.get("level")
            snap.change = level.get("change")
            snap.change_pct = level.get("change_pct")
            snap.day_high = level.get("high")
            snap.day_low = level.get("low")
            snap.volume = level.get("volume")
            snap.turnover = level.get("turnover")
            snap.is_official = True
            snap.basis = "Official value published by the Egyptian Exchange"
            snap.note = ""

    equal = definition["code"] != "EGX30"
    weighted_sum = 0.0
    weight_total = 0.0
    volume = 0.0
    turnover = 0.0
    seen_volume = False
    covered = 0

    for company in members:
        quote = quotes.get(company.ticker)
        if quote is None or quote.change_pct is None:
            continue
        covered += 1
        if quote.is_demo:
            snap.is_demo = True
        weight = _weight_for(company, quote, equal)
        if weight <= 0:
            weight = 1.0  # no share count: fall back to equal weight for this member
        weighted_sum += weight * float(quote.change_pct)
        weight_total += weight
        if quote.change_pct > 0:
            snap.advancers += 1
        elif quote.change_pct < 0:
            snap.decliners += 1
        else:
            snap.unchanged += 1
        if quote.volume:
            volume += float(quote.volume)
            seen_volume = True
        if quote.turnover:
            turnover += float(quote.turnover)

    snap.covered = covered
    if seen_volume and not snap.is_official:
        snap.volume = volume
        snap.turnover = turnover or None

    if not snap.is_official:
        if weight_total > 0:
            snap.change_pct = weighted_sum / weight_total
            snap.basis = (
                f"Composite of {covered} of {len(members)} constituents "
                f"({'equal' if equal else 'market-cap'} weighted)"
            )
            snap.note = UNOFFICIAL_NOTE
        else:
            snap.basis = "No constituent quotes available"
            snap.note = (
                "N/A — data unavailable. No quotes were available for this index's "
                "constituents."
            )

    snap.series = _composite_series(session, [c.ticker for c in members])
    return snap


# ---------------------------------------------------------------------------
# Movers and breadth
# ---------------------------------------------------------------------------
def _mover(company: Company, quote: Quote) -> MoverRow:
    return MoverRow(
        ticker=company.ticker, name=company.name, sector=company.sector,
        price=quote.price, change=quote.change, change_pct=quote.change_pct,
        volume=quote.volume, turnover=quote.turnover,
        is_demo=bool(quote.is_demo), badge=quote_freshness(quote)["badge"],
    )


def movers(
    companies: Sequence[Company], quotes: dict[str, Quote], *, limit: int = 10,
    min_turnover: float | None = None,
) -> tuple[list[MoverRow], list[MoverRow], list[MoverRow]]:
    """Top gainers, top losers, and most active by traded value.

    A "gainer" needs an actual move: rows without a change percentage are left
    out rather than sorted as zero.
    """
    rows: list[MoverRow] = []
    for company in companies:
        quote = quotes.get(company.ticker)
        if quote is None or quote.price is None:
            continue
        if min_turnover and (quote.turnover or 0) < min_turnover:
            continue
        rows.append(_mover(company, quote))

    priced = [r for r in rows if r.change_pct is not None]
    gainers = sorted([r for r in priced if r.change_pct > 0],
                     key=lambda r: r.change_pct, reverse=True)[:limit]
    losers = sorted([r for r in priced if r.change_pct < 0],
                    key=lambda r: r.change_pct)[:limit]
    active = sorted([r for r in rows if (r.turnover or r.volume)],
                    key=lambda r: (r.turnover or 0, r.volume or 0), reverse=True)[:limit]
    return gainers, losers, active


def breadth(companies: Sequence[Company], quotes: dict[str, Quote]) -> MarketBreadth:
    out = MarketBreadth(universe=len(companies))
    volume = 0.0
    turnover = 0.0
    seen_volume = False
    seen_turnover = False
    for company in companies:
        quote = quotes.get(company.ticker)
        if quote is None:
            continue
        out.covered += 1
        if quote.is_demo:
            out.is_demo = True
        if quote.change_pct is not None:
            if quote.change_pct > 0:
                out.advancers += 1
            elif quote.change_pct < 0:
                out.decliners += 1
            else:
                out.unchanged += 1
        if quote.volume:
            volume += float(quote.volume)
            seen_volume = True
        if quote.turnover:
            turnover += float(quote.turnover)
            seen_turnover = True
    out.total_volume = volume if seen_volume else None
    out.total_turnover = turnover if seen_turnover else None
    return out


# ---------------------------------------------------------------------------
# The dashboard payload
# ---------------------------------------------------------------------------
def market_overview(
    session: Session, *, limit: int = 10, refresh: bool = True,
    max_age_seconds: int = 120,
) -> MarketOverview:
    companies = active_universe(session)
    quotes = get_quotes(
        session, [c.ticker for c in companies],
        max_age_seconds=max_age_seconds, refresh=refresh,
    ) if companies else {}

    indices = [index_snapshot(session, d, quotes, companies) for d in INDEX_DEFINITIONS]
    gainers, losers, active = movers(companies, quotes, limit=limit)
    depth = breadth(companies, quotes)

    demo = any(q.is_demo for q in quotes.values())
    badges = {quote_freshness(q)["badge"] for q in quotes.values()}
    if not quotes:
        badge = "NO DATA"
        note = (
            "N/A — data unavailable. No market-data provider is currently returning "
            "quotes for the EGX universe."
        )
    elif demo:
        badge = "DEMO DATA"
        note = (
            "DEMO DATA — NOT REAL-TIME. These figures are generated for demonstration "
            "and are not real market prices. Connect a licensed EGX market-data "
            "provider to show real quotes."
        )
    elif "END OF DAY" in badges:
        badge = "END OF DAY"
        note = (
            "End-of-day prices from stored exchange data. Not a real-time feed."
        )
    elif "DELAYED" in badges:
        badge = "DELAYED"
        note = "Prices are delayed as permitted by the market-data licence in force."
    else:
        badge = "LIVE"
        note = "Real-time prices from the configured licensed market-data provider."

    stamps = [q.quote_time or q.retrieved_at for q in quotes.values()
              if (q.quote_time or q.retrieved_at)]
    return MarketOverview(
        indices=indices, gainers=gainers, losers=losers, most_active=active,
        breadth=depth, session=market_state(), data_note=note, is_demo=demo,
        quote_badge=badge, universe_size=len(companies), covered=len(quotes),
        as_of=max(stamps) if stamps else None,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@dataclass
class SearchHit:
    ticker: str
    name: str
    name_ar: str | None
    sector: str | None
    indices: list[str]
    price: float | None = None
    change_pct: float | None = None
    is_demo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def search_companies(
    session: Session, query: str, *, limit: int = 12, with_quotes: bool = False,
) -> list[SearchHit]:
    """Ticker, English name, Arabic name, and sector search.

    Exact ticker matches rank first, then ticker prefixes, then names — the
    order someone typing "COMI" expects.
    """
    term = (query or "").strip()
    if not term:
        return []
    pattern = f"%{term.lower()}%"

    rows = list(session.execute(
        select(Company).where(
            Company.status == "ACTIVE",
            or_(
                func.lower(Company.ticker).like(pattern),
                func.lower(Company.name).like(pattern),
                func.lower(func.coalesce(Company.name_ar, "")).like(f"%{term}%"),
                func.lower(func.coalesce(Company.sector, "")).like(pattern),
            ),
        ).limit(limit * 4)
    ).scalars().all())

    lowered = term.lower()

    def rank(company: Company) -> tuple[int, str]:
        ticker = company.ticker.lower()
        name = (company.name or "").lower()
        if ticker == lowered:
            return (0, ticker)
        if ticker.startswith(lowered):
            return (1, ticker)
        if name.startswith(lowered):
            return (2, name)
        if lowered in ticker:
            return (3, ticker)
        if lowered in name:
            return (4, name)
        return (5, ticker)

    rows.sort(key=rank)
    rows = rows[:limit]

    quotes: dict[str, Quote] = {}
    if with_quotes and rows:
        quotes = get_quotes(session, [c.ticker for c in rows], refresh=False)

    hits: list[SearchHit] = []
    for company in rows:
        indices = [code for code, flag in (
            ("EGX30", company.in_egx30), ("EGX70", company.in_egx70),
            ("EGX100", company.in_egx100),
        ) if flag]
        quote = quotes.get(company.ticker)
        hits.append(SearchHit(
            ticker=company.ticker, name=company.name, name_ar=company.name_ar,
            sector=company.sector, indices=indices,
            price=quote.price if quote else None,
            change_pct=quote.change_pct if quote else None,
            is_demo=bool(quote.is_demo) if quote else False,
        ))
    return hits
