"""Data ingestion engine.

Handles the operational realities the brief calls out:

* **Missing data** — rows lacking a usable value are skipped, never interpolated.
* **Duplicate data** — enforced by unique constraints and an upsert that only
  overwrites when the incoming record is genuinely better (see
  :func:`_should_replace`).
* **Market holidays** — an absent bar is an absent bar. No synthetic fill.
* **API failures** — the provider chain fails over; every failure is logged to
  ``data_quality_log`` so the UI can explain *why* data is missing.
* **Rate limits** — handled in the HTTP layer with backoff.

Historical data is never destroyed: an existing bar is replaced only by a
higher-confidence source or a genuinely more complete record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.data_quality import UNAVAILABLE, Confidence, safe_div
from backend.core.logging_config import (
    EVENT_DATA_UPDATE,
    EVENT_PROVIDER_FAILURE,
    get_logger,
    log_event,
)
from backend.data.models import (
    Company,
    DataQualityLog,
    Disclosure,
    FinancialStatement,
    NewsItem,
    PriceBar,
    ValuationSnapshot,
)
from backend.data.providers.base import (
    DisclosureDTO,
    FinancialStatementDTO,
    NewsDTO,
    PriceBarDTO,
)
from backend.data.providers.egx_disclosure import classify_disclosure, url_hash
from backend.data.providers.registry import (
    ProviderChain,
    disclosure_chain,
    fundamental_chain,
    market_data_chain,
    news_chain,
)
from backend.data.providers.rss_news import score_sentiment
from backend.data.universe import (
    get_universe,
    name_to_ticker_index,
    provider_symbol,
)

logger = get_logger(__name__)

#: Confidence ranking used when deciding whether a new record supersedes an old one.
_CONFIDENCE_RANK = {"UNVERIFIED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass
class IngestionResult:
    dataset: str
    ticker: str | None = None
    provider: str | None = None
    status: str = "OK"          # OK | PARTIAL | FAILED | SKIPPED
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "ticker": self.ticker,
            "provider": self.provider,
            "status": self.status,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "message": self.message,
        }


@dataclass
class IngestionSummary:
    results: list[IngestionResult] = field(default_factory=list)

    def add(self, result: IngestionResult) -> None:
        self.results.append(result)

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def updated(self) -> int:
        return sum(r.updated for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.results)

    @property
    def failures(self) -> list[IngestionResult]:
        return [r for r in self.results if r.status == "FAILED"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": len(self.failures),
            "results": [r.to_dict() for r in self.results],
        }


def _log_quality(session: Session, result: IngestionResult) -> None:
    session.add(
        DataQualityLog(
            dataset=result.dataset,
            ticker=result.ticker,
            provider=result.provider,
            status=result.status,
            rows_ingested=result.inserted + result.updated,
            rows_skipped=result.skipped,
            message=result.message or None,
        )
    )


def _should_replace(existing: PriceBar, incoming: PriceBarDTO, confidence: str) -> bool:
    """Only overwrite history when the new record is genuinely better.

    A higher-confidence source wins. Otherwise the incoming record must fill
    fields the stored one lacks. Equal-quality data never churns the database.
    """
    existing_rank = _CONFIDENCE_RANK.get((existing.confidence or "").upper(), 1)
    incoming_rank = _CONFIDENCE_RANK.get(confidence.upper(), 1)
    if incoming_rank > existing_rank:
        return True
    if incoming_rank < existing_rank:
        return False
    stored_missing = sum(
        1 for f in ("open", "high", "low", "adjusted_close", "volume")
        if getattr(existing, f) is None
    )
    incoming_present = sum(
        1 for f in ("open", "high", "low", "adjusted_close", "volume")
        if getattr(incoming, f) is not None
    )
    return stored_missing > 0 and incoming_present > (5 - stored_missing)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def ingest_prices_for_ticker(
    session: Session,
    ticker: str,
    *,
    chain: ProviderChain,
    start: date,
    end: date,
    company: Company | None = None,
) -> IngestionResult:
    result = IngestionResult(dataset="prices", ticker=ticker)
    hint = provider_symbol(company, "yahoo") if company else None

    bars, provider_name = chain.call(
        "get_price_history", ticker, start, end, symbol_hint=hint
    )
    result.provider = provider_name

    if not bars:
        result.status = "FAILED" if chain.errors else "SKIPPED"
        result.message = chain.error_summary()
        _log_quality(session, result)
        return result

    existing_rows = session.execute(
        select(PriceBar).where(
            PriceBar.ticker == ticker, PriceBar.timestamp >= start, PriceBar.timestamp <= end
        )
    ).scalars().all()
    existing = {row.timestamp: row for row in existing_rows}
    seen: set[date] = set()

    for bar in bars:
        if bar.close is None or bar.timestamp is None:
            result.skipped += 1
            continue
        if bar.timestamp in seen:  # duplicate within a single payload
            result.skipped += 1
            continue
        seen.add(bar.timestamp)

        quality = bar.quality
        source = quality.source if quality else (provider_name or "UNKNOWN")
        confidence = quality.confidence.value if quality else Confidence.HIGH.value
        retrieved = quality.retrieved_at if quality else datetime.now(timezone.utc)

        current = existing.get(bar.timestamp)
        if current is None:
            session.add(
                PriceBar(
                    company_id=company.company_id if company else None,
                    ticker=ticker,
                    timestamp=bar.timestamp,
                    open=bar.open, high=bar.high, low=bar.low,
                    close=bar.close, adjusted_close=bar.adjusted_close,
                    volume=bar.volume,
                    source=source, retrieved_at=retrieved, confidence=confidence,
                    data_period="1d",
                )
            )
            result.inserted += 1
        elif _should_replace(current, bar, confidence):
            current.open, current.high, current.low = bar.open, bar.high, bar.low
            current.close, current.adjusted_close = bar.close, bar.adjusted_close
            current.volume = bar.volume
            current.source, current.retrieved_at, current.confidence = source, retrieved, confidence
            result.updated += 1
        else:
            result.skipped += 1  # already have it at equal-or-better quality

    session.flush()
    result.message = f"{len(bars)} bars from {provider_name}"
    _log_quality(session, result)
    return result


def ingest_prices(
    session: Session,
    tickers: Sequence[str] | None = None,
    *,
    index: str = "all",
    lookback_days: int = 730,
    end: date | None = None,
    chain: ProviderChain | None = None,
) -> IngestionSummary:
    end = end or date.today()
    start = end - timedelta(days=lookback_days)
    owned_chain = chain is None
    if chain is None:
        chain = market_data_chain()
    summary = IngestionSummary()

    if not chain:
        result = IngestionResult(
            dataset="prices", status="FAILED",
            message="No market data provider is configured or available.",
        )
        summary.add(result)
        _log_quality(session, result)
        log_event(logger, EVENT_PROVIDER_FAILURE, result.message, dataset="prices")
        return summary

    companies = get_universe(session, index, include_benchmarks=True)
    if tickers:
        wanted = {t.upper() for t in tickers}
        companies = [c for c in companies if c.ticker in wanted]

    for company in companies:
        summary.add(
            ingest_prices_for_ticker(
                session, company.ticker, chain=chain, start=start, end=end, company=company
            )
        )

    log_event(
        logger, EVENT_DATA_UPDATE,
        f"Price ingestion: +{summary.inserted} new, ~{summary.updated} updated, "
        f"{summary.skipped} skipped, {len(summary.failures)} failed",
        dataset="prices", providers=",".join(chain.names),
    )
    if owned_chain:
        chain.close()
    return summary


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------
def ingest_fundamentals_for_ticker(
    session: Session, ticker: str, *, chain: ProviderChain, limit: int = 20
) -> IngestionResult:
    result = IngestionResult(dataset="fundamentals", ticker=ticker)
    statements, provider_name = chain.call("get_financial_statements", ticker, limit=limit)
    result.provider = provider_name

    if not statements:
        result.status = "FAILED" if chain.errors else "SKIPPED"
        result.message = chain.error_summary()
        _log_quality(session, result)
        return result

    existing_rows = session.execute(
        select(FinancialStatement).where(FinancialStatement.ticker == ticker)
    ).scalars().all()
    existing = {(row.period, row.period_type): row for row in existing_rows}

    for dto in statements:
        if dto.period_end is None:
            result.skipped += 1
            continue
        quality = dto.quality
        source = quality.source if quality else (provider_name or "UNKNOWN")
        confidence = quality.confidence.value if quality else Confidence.HIGH.value
        retrieved = quality.retrieved_at if quality else datetime.now(timezone.utc)

        fields = {
            "period_end": dto.period_end,
            # Without an explicit publication date, assume the EGX statutory
            # reporting window (~90 days). Conservative: never earlier than real.
            "available_from": dto.available_from or (dto.period_end + timedelta(days=90)),
            "revenue": dto.revenue, "gross_profit": dto.gross_profit, "ebitda": dto.ebitda,
            "operating_income": dto.operating_income, "net_income": dto.net_income,
            "eps": dto.eps, "cash": dto.cash, "total_debt": dto.total_debt,
            "total_assets": dto.total_assets, "total_equity": dto.total_equity,
            "operating_cash_flow": dto.operating_cash_flow, "capex": dto.capex,
            "free_cash_flow": dto.free_cash_flow, "interest_expense": dto.interest_expense,
            "current_assets": dto.current_assets, "current_liabilities": dto.current_liabilities,
            "dividends_paid": dto.dividends_paid,
            "source": source, "retrieved_at": retrieved, "confidence": confidence,
            "data_period": dto.period,
        }

        current = existing.get((dto.period, dto.period_type))
        if current is None:
            session.add(
                FinancialStatement(
                    ticker=ticker, period=dto.period, period_type=dto.period_type, **fields
                )
            )
            result.inserted += 1
        else:
            # Restatements are real; a fresher retrieval of the same period wins.
            for key, value in fields.items():
                setattr(current, key, value)
            result.updated += 1

    session.flush()
    result.message = f"{len(statements)} statements from {provider_name}"
    _log_quality(session, result)
    return result


def ingest_fundamentals(
    session: Session,
    tickers: Sequence[str] | None = None,
    *,
    index: str = "all",
    chain: ProviderChain | None = None,
) -> IngestionSummary:
    owned_chain = chain is None
    if chain is None:
        chain = fundamental_chain()
    summary = IngestionSummary()

    if not chain:
        result = IngestionResult(
            dataset="fundamentals", status="FAILED",
            message=(
                "No fundamental data provider is available. Free APIs do not cover "
                "EGX financial statements; supply CSV exports in data/manual/fundamentals/."
            ),
        )
        summary.add(result)
        _log_quality(session, result)
        return summary

    companies = get_universe(session, index)
    if tickers:
        wanted = {t.upper() for t in tickers}
        companies = [c for c in companies if c.ticker in wanted]

    for company in companies:
        summary.add(ingest_fundamentals_for_ticker(session, company.ticker, chain=chain))

    log_event(
        logger, EVENT_DATA_UPDATE,
        f"Fundamentals ingestion: +{summary.inserted} new, ~{summary.updated} updated, "
        f"{len(summary.failures)} failed",
        dataset="fundamentals", providers=",".join(chain.names),
    )
    if owned_chain:
        chain.close()
    return summary


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
def ingest_news(
    session: Session,
    tickers: Sequence[str] | None = None,
    *,
    limit: int = 200,
    since_days: int = 45,
    chain: ProviderChain | None = None,
) -> IngestionSummary:
    owned_chain = chain is None
    if chain is None:
        chain = news_chain()
    summary = IngestionSummary()
    result = IngestionResult(dataset="news")

    if not chain:
        result.status = "SKIPPED"
        result.message = "No news provider configured (add feeds to config/news_feeds.yaml)."
        summary.add(result)
        _log_quality(session, result)
        return summary

    # Give feed providers the name→ticker map so items can be attributed.
    index_map = name_to_ticker_index(session)
    for provider in chain.providers:
        if hasattr(provider, "set_ticker_index"):
            provider.set_ticker_index(index_map)

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    items, provider_name = chain.call("get_news", None, limit=limit, since=since)
    result.provider = provider_name

    if not items:
        result.status = "FAILED" if chain.errors else "SKIPPED"
        result.message = chain.error_summary()
        summary.add(result)
        _log_quality(session, result)
        if owned_chain:
            chain.close()
        return summary

    wanted = {t.upper() for t in tickers} if tickers else None
    for dto in items:
        if wanted and (dto.ticker or "").upper() not in wanted:
            result.skipped += 1
            continue
        digest = url_hash(dto.url or "", dto.title, dto.publication_date)
        if session.scalar(select(NewsItem).where(NewsItem.url_hash == digest)):
            result.skipped += 1
            continue

        sentiment, label = score_sentiment(f"{dto.title} {dto.summary or ''}")
        quality = dto.quality
        session.add(
            NewsItem(
                ticker=dto.ticker,
                title=dto.title[:2000],
                news_source=dto.source,
                url=dto.url,
                url_hash=digest,
                publication_date=dto.publication_date,
                summary=dto.summary,
                sentiment=sentiment,
                sentiment_label=label,
                importance=classify_disclosure(dto.title)[1],
                source=quality.source if quality else (provider_name or "UNKNOWN"),
                retrieved_at=quality.retrieved_at if quality else datetime.now(timezone.utc),
                confidence=quality.confidence.value if quality else Confidence.MEDIUM.value,
            )
        )
        result.inserted += 1

    session.flush()
    result.message = f"{len(items)} items from {provider_name}"
    summary.add(result)
    _log_quality(session, result)
    log_event(
        logger, EVENT_DATA_UPDATE,
        f"News ingestion: +{result.inserted} new, {result.skipped} duplicates/filtered",
        dataset="news",
    )
    if owned_chain:
        chain.close()
    return summary


# ---------------------------------------------------------------------------
# Disclosures
# ---------------------------------------------------------------------------
def ingest_disclosures(
    session: Session,
    tickers: Sequence[str] | None = None,
    *,
    limit: int = 200,
    since_days: int = 90,
    chain: ProviderChain | None = None,
) -> IngestionSummary:
    owned_chain = chain is None
    if chain is None:
        chain = disclosure_chain()
    summary = IngestionSummary()
    result = IngestionResult(dataset="disclosures")

    if not chain:
        result.status = "SKIPPED"
        result.message = "No disclosure provider configured."
        summary.add(result)
        _log_quality(session, result)
        return summary

    since = date.today() - timedelta(days=since_days)
    items, provider_name = chain.call("get_disclosures", None, limit=limit, since=since)
    result.provider = provider_name

    if not items:
        result.status = "FAILED" if chain.errors else "SKIPPED"
        result.message = chain.error_summary()
        summary.add(result)
        _log_quality(session, result)
        if owned_chain:
            chain.close()
        return summary

    wanted = {t.upper() for t in tickers} if tickers else None
    for dto in items:
        if wanted and (dto.ticker or "").upper() not in wanted:
            result.skipped += 1
            continue
        digest = url_hash(dto.url or "", dto.title, dto.date)
        if session.scalar(select(Disclosure).where(Disclosure.url_hash == digest)):
            result.skipped += 1
            continue

        kind, importance = classify_disclosure(dto.title)
        quality = dto.quality
        session.add(
            Disclosure(
                ticker=dto.ticker,
                title=dto.title[:2000],
                date=dto.date,
                disclosure_type=dto.disclosure_type or kind,
                url=dto.url,
                url_hash=digest,
                summary=dto.summary,
                importance=importance,
                source=quality.source if quality else (provider_name or "UNKNOWN"),
                retrieved_at=quality.retrieved_at if quality else datetime.now(timezone.utc),
                confidence=quality.confidence.value if quality else Confidence.HIGH.value,
            )
        )
        result.inserted += 1

    session.flush()
    result.message = f"{len(items)} disclosures from {provider_name}"
    summary.add(result)
    _log_quality(session, result)
    log_event(
        logger, EVENT_DATA_UPDATE,
        f"Disclosure ingestion: +{result.inserted} new, {result.skipped} duplicates/filtered",
        dataset="disclosures",
    )
    if owned_chain:
        chain.close()
    return summary


# ---------------------------------------------------------------------------
# Valuation snapshots (derived, not fetched)
# ---------------------------------------------------------------------------
def compute_valuation_snapshot(
    session: Session, ticker: str, as_of: date | None = None
) -> ValuationSnapshot | None:
    """Derive valuation multiples from stored price + fundamentals + share count.

    Every field is a CALCULATION over stored data. Any multiple whose inputs are
    missing stays ``None`` rather than being estimated.
    """
    as_of = as_of or date.today()
    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    bar = session.scalar(
        select(PriceBar)
        .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= as_of)
        .order_by(PriceBar.timestamp.desc())
    )
    statement = session.scalar(
        select(FinancialStatement)
        .where(
            FinancialStatement.ticker == ticker.upper(),
            FinancialStatement.period_type == "FY",
        )
        .order_by(FinancialStatement.period_end.desc())
    )
    if bar is None or bar.close is None:
        return None

    shares = company.shares_outstanding if company else None
    price = bar.close
    market_cap = price * shares if shares else None

    pe = pb = ps = ev_ebitda = ev_sales = fcf_yield = dividend_yield = None
    enterprise_value = None

    if statement is not None:
        # A negative denominator is rejected: a P/E on a loss-making company
        # is not a meaningful multiple, so it stays absent rather than negative.
        pe = _num(safe_div(price, statement.eps, allow_negative_denom=False))
        if market_cap:
            pb = _num(safe_div(market_cap, statement.total_equity, allow_negative_denom=False))
            ps = _num(safe_div(market_cap, statement.revenue, allow_negative_denom=False))
            net_debt = (statement.total_debt or 0.0) - (statement.cash or 0.0)
            enterprise_value = market_cap + net_debt
            ev_ebitda = _num(safe_div(enterprise_value, statement.ebitda, allow_negative_denom=False))
            ev_sales = _num(safe_div(enterprise_value, statement.revenue, allow_negative_denom=False))
            fcf_yield = _num(safe_div(statement.free_cash_flow, market_cap))
            if statement.dividends_paid:
                dividend_yield = _num(safe_div(abs(statement.dividends_paid), market_cap))

    existing = session.scalar(
        select(ValuationSnapshot).where(
            ValuationSnapshot.ticker == ticker.upper(), ValuationSnapshot.date == as_of
        )
    )
    payload = {
        "market_cap": market_cap, "enterprise_value": enterprise_value,
        "pe": pe, "pb": pb, "ps": ps, "ev_ebitda": ev_ebitda, "ev_sales": ev_sales,
        "fcf_yield": fcf_yield, "dividend_yield": dividend_yield,
        "source": "CALCULATED:price+fundamentals",
        "retrieved_at": datetime.now(timezone.utc),
        "confidence": (statement.confidence if statement else Confidence.LOW.value),
        "data_period": statement.period if statement else None,
    }
    if existing:
        for key, value in payload.items():
            setattr(existing, key, value)
        snapshot = existing
    else:
        snapshot = ValuationSnapshot(ticker=ticker.upper(), date=as_of, **payload)
        session.add(snapshot)
    session.flush()
    return snapshot


def _num(value: Any) -> float | None:
    """Convert an UNAVAILABLE-or-number into ``float | None`` for storage."""
    return None if value is UNAVAILABLE or value is None else float(value)
