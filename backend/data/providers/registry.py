"""Provider registry and failover chains.

Providers are declared by name in configuration and resolved here. Each dataset
gets a *chain* of providers tried in order; the first that returns data wins.
A provider that fails is logged and skipped — never silently substituted with a
default value.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import (
    BaseProvider,
    DisclosureProvider,
    FundamentalDataProvider,
    MarketDataProvider,
    NewsProvider,
    NullProvider,
    ProviderError,
)

logger = get_logger(__name__)

ProviderFactory = Callable[[], BaseProvider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name.lower()] = factory


def _install_defaults() -> None:
    if _REGISTRY:
        return

    def _csv() -> BaseProvider:
        from backend.data.providers.csv_provider import CsvFileProvider
        return CsvFileProvider()

    def _yahoo() -> BaseProvider:
        from backend.data.providers.yahoo import YahooFinanceProvider
        return YahooFinanceProvider()

    def _egx() -> BaseProvider:
        from backend.data.providers.egx_disclosure import EgxDisclosureProvider
        return EgxDisclosureProvider()

    def _rss() -> BaseProvider:
        from backend.data.providers.rss_news import RssNewsProvider
        return RssNewsProvider()

    def _synthetic() -> BaseProvider:
        from backend.data.providers.synthetic import SyntheticProvider
        return SyntheticProvider()

    register("csv", _csv)
    register("yahoo", _yahoo)
    register("egx", _egx)
    register("rss", _rss)
    register("synthetic", _synthetic)
    register("null", NullProvider)


def create_provider(name: str) -> BaseProvider | None:
    """Instantiate a provider by name, or ``None`` if unknown/unconstructable."""
    _install_defaults()
    factory = _REGISTRY.get(name.lower())
    if factory is None:
        logger.warning("Unknown provider %r — ignored", name)
        return None
    try:
        return factory()
    except ProviderError as exc:
        # e.g. synthetic disabled, or missing credentials. Expected, not fatal.
        log_event(
            logger, EVENT_PROVIDER_FAILURE,
            f"Provider {name!r} unavailable: {exc}", provider=name,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger, EVENT_PROVIDER_FAILURE,
            f"Provider {name!r} failed to construct: {exc}", provider=name,
        )
        return None


def build_chain(names: Sequence[str], required_type: type) -> list[Any]:
    """Instantiate *names*, keeping only those implementing *required_type*."""
    chain: list[Any] = []
    for name in names:
        provider = create_provider(name)
        if provider is None:
            continue
        if not isinstance(provider, required_type):
            logger.warning(
                "Provider %r does not implement %s — skipped", name, required_type.__name__
            )
            continue
        if not provider.is_available():
            log_event(
                logger, EVENT_PROVIDER_FAILURE,
                f"Provider {name!r} not available: {provider.unavailable_reason()}",
                provider=name,
            )
            continue
        chain.append(provider)
    return chain


class ProviderChain:
    """Tries each provider in order; returns the first non-empty result.

    Failures are recorded in :attr:`errors` so callers can report *why* nothing
    was found rather than presenting an empty result as a fact about the market.
    """

    def __init__(self, providers: Iterable[Any], dataset: str) -> None:
        self.providers = list(providers)
        self.dataset = dataset
        self.errors: list[tuple[str, str]] = []

    def __bool__(self) -> bool:
        return bool(self.providers)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    @property
    def uses_synthetic(self) -> bool:
        return any(getattr(p, "is_synthetic", False) for p in self.providers)

    def call(self, method: str, *args: Any, **kwargs: Any) -> tuple[Any, str | None]:
        """Invoke *method* across the chain. Returns ``(result, provider_name)``."""
        self.errors.clear()
        for provider in self.providers:
            func = getattr(provider, method, None)
            if func is None:
                continue
            try:
                result = func(*args, **kwargs)
            except ProviderError as exc:
                self.errors.append((provider.name, str(exc)))
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"{provider.name}.{method} failed: {exc}",
                    provider=provider.name, dataset=self.dataset,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a bad provider must not halt ingestion
                self.errors.append((provider.name, f"{exc.__class__.__name__}: {exc}"))
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"{provider.name}.{method} raised unexpectedly: {exc}",
                    provider=provider.name, dataset=self.dataset,
                )
                continue
            if result:
                return result, provider.name
        return None, None

    def close(self) -> None:
        for provider in self.providers:
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass

    def error_summary(self) -> str:
        if not self.errors:
            return "no provider returned data"
        return "; ".join(f"{name}: {msg}" for name, msg in self.errors)


# ---------------------------------------------------------------------------
# Configured chains
# ---------------------------------------------------------------------------
def market_data_chain() -> ProviderChain:
    s = get_settings()
    return ProviderChain(build_chain(s.market_data_providers, MarketDataProvider), "prices")


def fundamental_chain() -> ProviderChain:
    s = get_settings()
    return ProviderChain(
        build_chain(s.fundamental_providers, FundamentalDataProvider), "fundamentals"
    )


def news_chain() -> ProviderChain:
    s = get_settings()
    return ProviderChain(build_chain(s.news_providers, NewsProvider), "news")


def disclosure_chain() -> ProviderChain:
    s = get_settings()
    return ProviderChain(build_chain(s.disclosure_providers, DisclosureProvider), "disclosures")


def provider_status() -> list[dict[str, Any]]:
    """Introspection for the Settings page: what is configured and usable."""
    _install_defaults()
    settings = get_settings()
    configured = {
        "prices": settings.market_data_providers,
        "fundamentals": settings.fundamental_providers,
        "news": settings.news_providers,
        "disclosures": settings.disclosure_providers,
    }
    rows: list[dict[str, Any]] = []
    for dataset, names in configured.items():
        for name in names:
            provider = create_provider(name)
            if provider is None:
                rows.append({
                    "dataset": dataset, "provider": name, "available": False,
                    "synthetic": name == "synthetic",
                    "reason": "Provider could not be constructed (see logs).",
                    "notes": "",
                })
                continue
            caps = provider.capabilities()
            rows.append({
                "dataset": dataset,
                "provider": name,
                "available": provider.is_available(),
                "synthetic": caps.is_synthetic,
                "reason": provider.unavailable_reason() or "",
                "notes": caps.notes,
            })
            provider.close()
    return rows
