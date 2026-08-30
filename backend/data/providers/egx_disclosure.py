"""Official EGX disclosure provider.

Reads the Egyptian Exchange's public disclosure listing. EGX has changed its
site structure several times, so this provider tries several known endpoints and
falls back to parsing the HTML listing. When none respond it raises
:class:`ProviderUnavailable` — it never invents a disclosure.

If EGX changes its markup, override ``EGX_DISCLOSURE_BASE_URL`` or supply
disclosures through the CSV provider instead.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from backend.core.config import get_settings
from backend.core.data_quality import Confidence
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import (
    DisclosureDTO,
    DisclosureProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderUnavailable,
)
from backend.data.providers.csv_provider import parse_date
from backend.data.providers.http_client import HttpFetcher

logger = get_logger(__name__)

#: Tried in order. The first that returns parseable content wins.
CANDIDATE_PATHS = (
    "/api/disclosures/latest",
    "/en/disclosures.aspx",
    "/English/Disclosures.aspx",
)

#: Keyword → (type, importance 1-5). Drives event classification downstream.
DISCLOSURE_TYPES: tuple[tuple[str, str, int], ...] = (
    ("acquisi", "M&A", 5),
    ("merger", "M&A", 5),
    ("tender offer", "M&A", 5),
    ("capital increase", "CAPITAL_ACTION", 4),
    ("capital decrease", "CAPITAL_ACTION", 4),
    ("buyback", "BUYBACK", 4),
    ("treasury share", "BUYBACK", 4),
    ("dividend", "DIVIDEND", 4),
    ("coupon", "DIVIDEND", 3),
    ("financial statement", "EARNINGS", 5),
    ("financial result", "EARNINGS", 5),
    ("earnings", "EARNINGS", 5),
    ("board of directors", "GOVERNANCE", 3),
    ("resignation", "MANAGEMENT_CHANGE", 4),
    ("appointment", "MANAGEMENT_CHANGE", 3),
    ("chief executive", "MANAGEMENT_CHANGE", 4),
    ("contract", "CONTRACT", 4),
    ("award", "CONTRACT", 3),
    ("restructur", "RESTRUCTURING", 4),
    ("suspension", "REGULATORY", 5),
    ("delisting", "REGULATORY", 5),
    ("general assembly", "GOVERNANCE", 2),
)


def classify_disclosure(title: str) -> tuple[str, int]:
    """Map a headline to (type, importance). Unmatched → ('OTHER', 2)."""
    lowered = (title or "").lower()
    for keyword, kind, importance in DISCLOSURE_TYPES:
        if keyword in lowered:
            return kind, importance
    return "OTHER", 2


def url_hash(*parts: Any) -> str:
    """Stable identity for de-duplication when a source gives no unique id."""
    joined = "|".join(str(p) for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:48]


class EgxDisclosureProvider(DisclosureProvider):
    name = "egx"

    def __init__(self, fetcher: HttpFetcher | None = None) -> None:
        settings = get_settings()
        self._fetcher = fetcher or HttpFetcher(base_url=settings.disclosure_base_url)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains={"disclosures"},
            requires_credentials=False,
            notes=(
                "Official EGX disclosure listing. Endpoint structure changes "
                "periodically; falls back to the CSV provider when unreachable."
            ),
        )

    def get_disclosures(
        self, ticker: str | None = None, *, limit: int = 50, since: date | None = None
    ) -> list[DisclosureDTO]:
        last_error: Exception | None = None
        for path in CANDIDATE_PATHS:
            try:
                response = self._fetcher.get(path)
            except ProviderError as exc:
                last_error = exc
                continue

            content_type = response.headers.get("content-type", "")
            try:
                if "json" in content_type:
                    items = self._parse_json(response.json())
                else:
                    items = self._parse_html(response.text)
            except Exception as exc:  # noqa: BLE001 - any parse failure -> try next
                last_error = exc
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"Failed to parse EGX disclosures from {path}: {exc}", provider=self.name,
                )
                continue

            if items:
                filtered = [
                    d for d in items
                    if (not ticker or (d.ticker or "").upper() == ticker.upper())
                    and (not since or not d.date or d.date >= since)
                ]
                return filtered[:limit]

        raise ProviderUnavailable(
            f"EGX disclosure endpoints unreachable or unparseable: {last_error}"
        )

    # -- parsers --------------------------------------------------------------
    def _parse_json(self, payload: Any) -> list[DisclosureDTO]:
        rows = payload
        if isinstance(payload, dict):
            for key in ("data", "items", "disclosures", "result", "results"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
        if not isinstance(rows, list):
            return []

        quality = self.quality(confidence=Confidence.HIGH)
        out: list[DisclosureDTO] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lowered = {str(k).lower(): v for k, v in row.items()}
            title = (
                lowered.get("title")
                or lowered.get("subject")
                or lowered.get("disclosurename")
                or lowered.get("news")
            )
            if not title:
                continue
            kind, importance = classify_disclosure(str(title))
            out.append(
                DisclosureDTO(
                    ticker=_clean_ticker(
                        lowered.get("ticker") or lowered.get("symbol") or lowered.get("code")
                    ),
                    title=str(title).strip(),
                    date=parse_date(
                        lowered.get("date") or lowered.get("disclosuredate") or lowered.get("publishdate")
                    ),
                    disclosure_type=str(lowered.get("type") or kind),
                    url=_absolute(str(lowered.get("url") or lowered.get("link") or ""), self._fetcher.base_url),
                    summary=lowered.get("summary") or lowered.get("description"),
                    quality=quality,
                )
            )
        return out

    def _parse_html(self, html: str) -> list[DisclosureDTO]:
        """Parse the disclosure table without assuming an exact DOM shape."""
        try:
            from lxml import html as lxml_html
        except ImportError:  # pragma: no cover
            return []

        tree = lxml_html.fromstring(html)
        quality = self.quality(confidence=Confidence.MEDIUM)
        out: list[DisclosureDTO] = []

        for row in tree.xpath("//table//tr"):
            cells = [c.text_content().strip() for c in row.xpath("./td")]
            if len(cells) < 2:
                continue
            found_date = next((parse_date(c) for c in cells if parse_date(c)), None)
            title = max((c for c in cells if not parse_date(c)), key=len, default="")
            if not title or len(title) < 8:
                continue
            links = row.xpath(".//a/@href")
            kind, importance = classify_disclosure(title)
            out.append(
                DisclosureDTO(
                    ticker=_extract_ticker(cells),
                    title=title,
                    date=found_date,
                    disclosure_type=kind,
                    url=_absolute(links[0] if links else "", self._fetcher.base_url),
                    summary=None,
                    quality=quality,
                )
            )
        return out

    def close(self) -> None:
        self._fetcher.close()


_TICKER_RE = re.compile(r"^[A-Z]{3,6}(\.CA)?$")


def _clean_ticker(value: Any) -> str | None:
    if not value:
        return None
    t = str(value).strip().upper().replace(".CA", "")
    return t if _TICKER_RE.match(t) else None


def _extract_ticker(cells: list[str]) -> str | None:
    for cell in cells:
        cleaned = _clean_ticker(cell)
        if cleaned:
            return cleaned
    return None


def _absolute(url: str, base: str) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{base.rstrip('/')}/{url.lstrip('/')}"
