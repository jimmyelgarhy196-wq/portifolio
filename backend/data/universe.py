"""EGX investable universe management.

Loads the seed from ``config/universe.yaml`` into the ``companies`` table and
provides selection helpers. The seed holds reference data only — ticker, name,
sector, index membership — never prices or financials.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import load_yaml_config
from backend.core.logging_config import EVENT_DATA_UPDATE, get_logger, log_event
from backend.data.models import Company

logger = get_logger(__name__)

INDEX_EGX30 = "egx30"
INDEX_EGX70 = "egx70"
INDEX_EGX100 = "egx100"
INDEX_ALL = "all"


@dataclass
class UniverseStatus:
    verified: bool
    as_of: str | None
    source: str
    note: str
    company_count: int

    @property
    def warning(self) -> str | None:
        """Shown in the UI while the universe has not been reconciled with EGX."""
        if self.verified:
            return None
        return (
            "UNVERIFIED UNIVERSE — ticker, sector and index membership come from a "
            "reference seed that has not been reconciled against an official EGX "
            "constituent list. Verify before relying on index membership."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "as_of": self.as_of,
            "source": self.source,
            "note": self.note,
            "company_count": self.company_count,
            "warning": self.warning,
        }


def universe_meta() -> dict[str, Any]:
    return load_yaml_config("universe").get("meta") or {}


def universe_status(session: Session) -> UniverseStatus:
    meta = universe_meta()
    count = session.scalar(
        select(func.count()).select_from(Company).where(Company.status != "INDEX")
    ) or 0
    return UniverseStatus(
        verified=bool(meta.get("verified", False)),
        as_of=meta.get("as_of"),
        source=str(meta.get("source", "unknown")),
        note=str(meta.get("note", "")),
        company_count=count,
    )


def load_universe(session: Session, *, update_existing: bool = True) -> dict[str, int]:
    """Upsert the configured universe into ``companies``. Idempotent."""
    config = load_yaml_config("universe")
    entries: list[dict[str, Any]] = list(config.get("companies") or [])
    benchmarks: list[dict[str, Any]] = list(config.get("benchmarks") or [])

    created = updated = 0
    for entry in entries + benchmarks:
        ticker = str(entry.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        existing = session.scalar(select(Company).where(Company.ticker == ticker))
        symbols = dict(entry.get("provider_symbols") or {})
        # Yahoo's EGX convention: TICKER.CA. Indices carry explicit symbols.
        if "yahoo" not in symbols and not entry.get("is_benchmark"):
            symbols["yahoo"] = f"{ticker}.CA"

        fields = {
            "name": entry.get("name") or ticker,
            "sector": entry.get("sector") or ("Index" if entry.get("is_benchmark") else None),
            "industry": entry.get("industry"),
            "exchange": entry.get("exchange", "EGX"),
            "currency": entry.get("currency", "EGP"),
            "status": entry.get("status", "INDEX" if entry.get("is_benchmark") else "ACTIVE"),
            "in_egx30": bool(entry.get("in_egx30", False)),
            "in_egx70": bool(entry.get("in_egx70", False)),
            # EGX100 is the union of EGX30 and EGX70 by construction.
            "in_egx100": bool(
                entry.get("in_egx100", entry.get("in_egx30", False) or entry.get("in_egx70", False))
            ),
            "provider_symbols": symbols,
        }

        if existing is None:
            session.add(Company(ticker=ticker, **fields))
            created += 1
        elif update_existing:
            for key, value in fields.items():
                setattr(existing, key, value)
            updated += 1

    session.flush()
    log_event(
        logger, EVENT_DATA_UPDATE,
        f"Universe loaded: {created} created, {updated} updated",
        dataset="universe", created=created, updated=updated,
    )
    return {"created": created, "updated": updated, "total": created + updated}


def get_universe(
    session: Session,
    index: str = INDEX_ALL,
    *,
    include_benchmarks: bool = False,
    sectors: Sequence[str] | None = None,
) -> list[Company]:
    """Select companies by index membership and optional sector filter."""
    stmt = select(Company)
    if not include_benchmarks:
        stmt = stmt.where(Company.status != "INDEX")

    key = (index or INDEX_ALL).lower()
    if key == INDEX_EGX30:
        stmt = stmt.where(Company.in_egx30.is_(True))
    elif key == INDEX_EGX70:
        stmt = stmt.where(Company.in_egx70.is_(True))
    elif key == INDEX_EGX100:
        stmt = stmt.where(Company.in_egx100.is_(True))

    if sectors:
        stmt = stmt.where(Company.sector.in_(list(sectors)))

    return list(session.execute(stmt.order_by(Company.ticker)).scalars().all())


def get_company(session: Session, ticker: str) -> Company | None:
    return session.scalar(select(Company).where(Company.ticker == ticker.upper()))


def get_tickers(session: Session, index: str = INDEX_ALL) -> list[str]:
    return [c.ticker for c in get_universe(session, index)]


def name_to_ticker_index(session: Session) -> dict[str, str]:
    """Build {company name/alias → ticker} for news matching."""
    mapping: dict[str, str] = {}
    for company in get_universe(session, INDEX_ALL):
        mapping[company.name.lower()] = company.ticker
        # A shortened alias helps match headlines that omit the legal suffix.
        head = company.name.split("(")[0].strip()
        if len(head) >= 6:
            mapping[head.lower()] = company.ticker
    return mapping


def sectors(session: Session) -> list[str]:
    rows = session.execute(
        select(Company.sector).where(Company.sector.isnot(None)).distinct()
    ).scalars().all()
    return sorted(r for r in rows if r and r != "Index")


def provider_symbol(company: Company, provider: str) -> str | None:
    return (company.provider_symbols or {}).get(provider)
