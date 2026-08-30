"""RSS/Atom news provider.

Reads configurable feeds and matches items to EGX tickers by company name and
ticker mention. Sentiment is a transparent lexicon score, deliberately simple and
labelled as such — it is a heuristic, never presented as a fact about a company.

Feeds are configured in ``config/news_feeds.yaml``. No feeds are enabled by
default, because shipping a hardcoded list of news sources would silently decide
what this system reads.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from backend.core.config import load_yaml_config
from backend.core.data_quality import Confidence
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import (
    NewsDTO,
    NewsProvider,
    ProviderCapabilities,
    ProviderError,
)
from backend.data.providers.http_client import HttpFetcher

logger = get_logger(__name__)

# --- Transparent sentiment lexicon ------------------------------------------
# This is a heuristic, not an assessment. It is surfaced in the UI as
# "lexicon sentiment" and never described as a fact about the company.
POSITIVE_TERMS = {
    "profit": 1.0, "profits": 1.0, "growth": 0.8, "grew": 0.8, "rise": 0.7, "rose": 0.7,
    "surge": 1.2, "surged": 1.2, "gain": 0.7, "gains": 0.7, "record": 1.0, "beat": 1.0,
    "upgrade": 1.1, "upgraded": 1.1, "expansion": 0.8, "expand": 0.7, "dividend": 0.6,
    "buyback": 0.9, "acquisition": 0.5, "contract": 0.7, "award": 0.8, "awarded": 0.8,
    "approval": 0.7, "approved": 0.7, "outperform": 1.1, "strong": 0.8, "improved": 0.8,
    "increase": 0.6, "increased": 0.6, "higher": 0.6, "boost": 0.8, "recovery": 0.7,
}
NEGATIVE_TERMS = {
    "loss": -1.0, "losses": -1.0, "decline": -0.8, "declined": -0.8, "fall": -0.7,
    "fell": -0.7, "drop": -0.8, "dropped": -0.8, "plunge": -1.2, "plunged": -1.2,
    "downgrade": -1.1, "downgraded": -1.1, "lawsuit": -1.0, "probe": -0.9,
    "investigation": -0.9, "fine": -0.8, "fined": -0.8, "penalty": -0.8,
    "suspension": -1.2, "suspended": -1.2, "delay": -0.7, "delayed": -0.7,
    "resign": -0.8, "resigned": -0.8, "default": -1.3, "restructuring": -0.5,
    "weak": -0.8, "weaker": -0.8, "cut": -0.7, "warning": -0.9, "impairment": -1.0,
    "lower": -0.6, "miss": -0.9, "missed": -0.9, "halt": -1.0, "halted": -1.0,
}


def score_sentiment(text: str) -> tuple[float | None, str]:
    """Return (score in [-1, 1], label). ``None`` when no lexicon term matched.

    Returning ``None`` rather than 0.0 matters: "no signal" and "neutral signal"
    are different claims, and conflating them would be a small fabrication.
    """
    if not text:
        return None, "UNKNOWN"
    words = re.findall(r"[a-z']+", text.lower())
    total = 0.0
    hits = 0
    for word in words:
        if word in POSITIVE_TERMS:
            total += POSITIVE_TERMS[word]
            hits += 1
        elif word in NEGATIVE_TERMS:
            total += NEGATIVE_TERMS[word]
            hits += 1
    if hits == 0:
        return None, "UNKNOWN"
    score = max(-1.0, min(1.0, total / (hits * 1.2)))
    if score > 0.25:
        label = "POSITIVE"
    elif score < -0.25:
        label = "NEGATIVE"
    else:
        label = "NEUTRAL"
    return round(score, 3), label


class RssNewsProvider(NewsProvider):
    name = "rss"

    def __init__(
        self,
        feeds: list[dict[str, Any]] | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> None:
        cfg = load_yaml_config("news_feeds")
        self.feeds = feeds if feeds is not None else (cfg.get("feeds") or [])
        self._fetcher = fetcher or HttpFetcher()
        self._name_index: dict[str, str] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains={"news"},
            requires_credentials=False,
            notes=f"{len(self.feeds)} feed(s) configured in config/news_feeds.yaml",
        )

    def is_available(self) -> bool:
        return bool(self.feeds)

    def unavailable_reason(self) -> str | None:
        if not self.feeds:
            return "No RSS feeds configured. Add them to config/news_feeds.yaml."
        return None

    def set_ticker_index(self, mapping: dict[str, str]) -> None:
        """Provide {company name or alias (lowercase): ticker} for matching."""
        self._name_index = {k.lower(): v for k, v in mapping.items()}

    def get_news(
        self, ticker: str | None = None, *, limit: int = 50, since: datetime | None = None
    ) -> list[NewsDTO]:
        items: list[NewsDTO] = []
        for feed in self.feeds:
            url = feed.get("url") if isinstance(feed, dict) else str(feed)
            if not url:
                continue
            try:
                response = self._fetcher.get(url)
            except ProviderError as exc:
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"News feed unreachable: {exc}", provider=self.name, url=url,
                )
                continue
            source_name = (feed.get("name") if isinstance(feed, dict) else None) or _host(url)
            items.extend(self._parse_feed(response.text, source_name))

        if ticker:
            items = [i for i in items if (i.ticker or "").upper() == ticker.upper()]
        if since:
            items = [i for i in items if not i.publication_date or i.publication_date >= since]
        items.sort(
            key=lambda n: n.publication_date or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return items[:limit]

    def _parse_feed(self, xml_text: str, source_name: str) -> list[NewsDTO]:
        try:
            from lxml import etree
        except ImportError:  # pragma: no cover
            return []
        try:
            root = etree.fromstring(xml_text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger, EVENT_PROVIDER_FAILURE,
                f"Malformed feed XML from {source_name}: {exc}", provider=self.name,
            )
            return []

        quality = self.quality(confidence=Confidence.MEDIUM)
        out: list[NewsDTO] = []
        # RSS <item> and Atom <entry>
        nodes = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )
        for node in nodes:
            title = _text(node, "title")
            if not title:
                continue
            link = _text(node, "link") or _attr(node, "link", "href")
            summary = _text(node, "description") or _text(node, "summary")
            published = _parse_feed_date(
                _text(node, "pubDate") or _text(node, "published") or _text(node, "updated")
            )
            out.append(
                NewsDTO(
                    ticker=self._match_ticker(f"{title} {summary or ''}"),
                    title=title.strip(),
                    source=source_name,
                    url=link,
                    publication_date=published,
                    summary=(summary or "").strip() or None,
                    quality=quality,
                )
            )
        return out

    def _match_ticker(self, text: str) -> str | None:
        """Match by company name or explicit ticker mention. No fuzzy guessing."""
        lowered = text.lower()
        for name, ticker in self._name_index.items():
            if len(name) >= 4 and name in lowered:
                return ticker
        for ticker in set(self._name_index.values()):
            if re.search(rf"\b{re.escape(ticker)}\b", text, re.IGNORECASE):
                return ticker
        return None

    def close(self) -> None:
        self._fetcher.close()


def _text(node: Any, tag: str) -> str | None:
    for candidate in (tag, f"{{http://www.w3.org/2005/Atom}}{tag}"):
        found = node.find(candidate)
        if found is not None and found.text:
            return found.text
    return None


def _attr(node: Any, tag: str, attribute: str) -> str | None:
    for candidate in (tag, f"{{http://www.w3.org/2005/Atom}}{tag}"):
        found = node.find(candidate)
        if found is not None and found.get(attribute):
            return found.get(attribute)
    return None


def _parse_feed_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1) if match else url
