"""Real-time quote providers: the wire between this platform and a paid feed.

This module is what makes the platform live. Everything else — scores, DCF,
screens, alerts, the weekly report — already works; it was reading generated
demonstration prices because there was no feed attached. Attach one here and
the whole system runs on real EGX quotes without another line of code.

Design, in one sentence: **a vendor is data, not code.** A provider is described
by a :class:`VendorSpec` — a URL template, an auth style, and a map from vendor
JSON keys onto :class:`~backend.market.quotes.QuoteData` fields. Two presets
ship (:data:`EODHD` and :data:`TWELVE_DATA`); any other vendor is a JSON file,
not a pull request. That matters because you cannot test a vendor's exact
response shape until you hold their key, and a shape surprise must be a config
edit at 9am, not a code change and a redeploy.

The honesty rules from the rest of the codebase are enforced here too, because
this is the module where fabricated data would enter if it ever could:

* A row without a usable positive price is **dropped**, not defaulted. A vendor
  returning ``null`` for a suspended stock must produce "N/A", never 0.00.
* A row whose currency is not the expected one is **dropped**. A USD price
  rendered under an EGP label is a wrong number, not a formatting problem.
* ``is_demo`` is hard-coded ``False`` on this path and ``True`` on the demo
  path. The flag is never computed from anything a vendor sends.
* ``delayed_minutes`` comes from :attr:`Settings.quote_delay_minutes` — the
  delay your licence actually grants — not from a vendor's marketing claim. The
  vendor's own timestamp is stored separately as ``quote_time`` so the UI can
  show the true age of the print.
* No vendor's terminal or website is scraped. These are documented JSON APIs
  called with a key you pay for.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import ProviderUnavailable
from backend.data.providers.http_client import HttpFetcher
from backend.market.status import market_state

logger = get_logger(__name__)

#: Fields a spec may map. Anything not listed is ignored rather than guessed.
MAPPABLE_FIELDS = (
    "price", "previous_close", "open", "day_high", "day_low",
    "volume", "turnover", "trades", "week52_high", "week52_low",
    "market_cap", "currency", "quote_time",
)


# ---------------------------------------------------------------------------
# Vendor description
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VendorSpec:
    """Everything needed to call one vendor, expressed as data.

    ``url`` is a template. ``{symbols}`` is replaced by the joined vendor
    symbols for a batch request, ``{symbol}`` by a single one, and ``{key}`` by
    the API key (only for vendors that put the key in the path — most take it as
    a query parameter, which is what ``auth_param`` is for).
    """

    name: str
    display_name: str
    url: str
    #: "query" puts the key in a query parameter, "header" in a request header,
    #: "path" means the URL template contains {key}, "none" for open endpoints.
    auth: str = "query"
    auth_param: str = "apikey"
    auth_header: str = "Authorization"
    auth_header_format: str = "Bearer {key}"
    #: Extra static query parameters the vendor requires (e.g. {"fmt": "json"}).
    params: dict[str, str] = field(default_factory=dict)

    #: True when the vendor accepts several symbols in one call.
    batch: bool = False
    batch_separator: str = ","
    max_batch: int = 1

    #: Dotted path to the list (or dict) of quote rows. "" means the response
    #: root is itself the row or the list of rows.
    root: str = ""
    #: Key inside a row holding the vendor's symbol, used to match rows back to
    #: tickers on a batch response.
    symbol_key: str = "code"

    #: QuoteData field -> dotted path within a row.
    fields: dict[str, str] = field(default_factory=dict)

    #: How to read the timestamp named by fields["quote_time"].
    time_format: str = "epoch"  # "epoch" | "iso" | "none"

    #: Appended to the GMG ticker to form the vendor symbol (e.g. "COMI" ->
    #: "COMI.EGX"). Per-ticker overrides live in the symbol map file.
    symbol_suffix: str = ""

    #: Currency every row is expected to be quoted in. A row that disagrees is
    #: dropped. Empty string disables the check.
    expect_currency: str = "EGP"

    #: Documentation URL, shown in the admin data-source panel.
    docs: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "display_name": self.display_name, "url": self.url,
            "auth": self.auth, "auth_param": self.auth_param,
            "auth_header": self.auth_header,
            "auth_header_format": self.auth_header_format,
            "params": dict(self.params), "batch": self.batch,
            "batch_separator": self.batch_separator, "max_batch": self.max_batch,
            "root": self.root, "symbol_key": self.symbol_key,
            "fields": dict(self.fields), "time_format": self.time_format,
            "symbol_suffix": self.symbol_suffix,
            "expect_currency": self.expect_currency, "docs": self.docs,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VendorSpec":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"Unknown key(s) in vendor spec: {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(known))}."
            )
        missing = {"name", "url"} - set(raw)
        if missing:
            raise ValueError(f"Vendor spec is missing required key(s): {sorted(missing)}")
        bad = set(raw.get("fields", {})) - set(MAPPABLE_FIELDS)
        if bad:
            raise ValueError(
                f"Vendor spec maps unknown field(s): {', '.join(sorted(bad))}. "
                f"Mappable fields: {', '.join(MAPPABLE_FIELDS)}."
            )
        if "price" not in raw.get("fields", {}):
            raise ValueError(
                "Vendor spec must map 'price'. A quote provider that cannot "
                "produce a price is not a quote provider."
            )
        raw = dict(raw)
        raw.setdefault("display_name", raw["name"])
        return cls(**raw)


# ---------------------------------------------------------------------------
# Presets
#
# These describe the vendors' documented JSON responses. They are starting
# points, not guarantees: response shapes change and accounts differ by plan.
# `python scripts/verify_market_data.py` prints the raw response beside the
# parsed quote so a mismatch is visible in one command, and any mismatch is
# fixed by editing the spec — no code change.
# ---------------------------------------------------------------------------
EODHD = VendorSpec(
    name="eodhd",
    display_name="EODHD",
    url="https://eodhd.com/api/real-time/{symbol}",
    auth="query",
    auth_param="api_token",
    params={"fmt": "json"},
    batch=True,
    batch_separator=",",
    max_batch=20,
    root="",
    symbol_key="code",
    fields={
        "price": "close",
        "previous_close": "previousClose",
        "open": "open",
        "day_high": "high",
        "day_low": "low",
        "volume": "volume",
        "quote_time": "timestamp",
    },
    time_format="epoch",
    symbol_suffix=".EGX",
    docs="https://eodhd.com/financial-apis/live-realtime-stocks-api",
)

TWELVE_DATA = VendorSpec(
    name="twelvedata",
    display_name="Twelve Data",
    url="https://api.twelvedata.com/quote",
    auth="query",
    auth_param="apikey",
    params={},
    batch=True,
    batch_separator=",",
    max_batch=8,
    root="",
    symbol_key="symbol",
    fields={
        "price": "close",
        "previous_close": "previous_close",
        "open": "open",
        "day_high": "high",
        "day_low": "low",
        "volume": "volume",
        "week52_high": "fifty_two_week.high",
        "week52_low": "fifty_two_week.low",
        "currency": "currency",
        "quote_time": "timestamp",
    },
    time_format="epoch",
    symbol_suffix=":EGX",
    docs="https://twelvedata.com/docs#quote",
)

PRESETS: dict[str, VendorSpec] = {
    EODHD.name: EODHD,
    TWELVE_DATA.name: TWELVE_DATA,
}


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------
class SymbolMapper:
    """Translates GMG tickers to vendor symbols, and back.

    Default rule is ticker + the spec's suffix. Exceptions — and there always
    are some, because vendors disagree about EGX symbology — go in a JSON file
    keyed by vendor name, so a wrong symbol is a data fix rather than a deploy.
    """

    def __init__(self, spec: VendorSpec, overrides: dict[str, str] | None = None) -> None:
        self.spec = spec
        self._to_vendor = {k.upper(): v for k, v in (overrides or {}).items()}
        self._to_ticker = {v.upper(): k for k, v in self._to_vendor.items()}

    @classmethod
    def load(cls, spec: VendorSpec, path: str | os.PathLike[str] | None = None) -> "SymbolMapper":
        path = path or get_settings().symbol_map_path
        overrides: dict[str, str] = {}
        if path:
            p = Path(path)
            if p.exists():
                try:
                    raw = json.loads(p.read_text())
                except ValueError as exc:
                    raise ProviderUnavailable(
                        f"Symbol map {p} is not valid JSON: {exc}"
                    ) from exc
                overrides = {
                    str(k): str(v) for k, v in (raw.get(spec.name) or {}).items()
                }
        return cls(spec, overrides)

    def to_vendor(self, ticker: str) -> str:
        ticker = ticker.upper()
        return self._to_vendor.get(ticker, f"{ticker}{self.spec.symbol_suffix}")

    def to_ticker(self, vendor_symbol: str) -> str:
        symbol = (vendor_symbol or "").upper()
        if symbol in self._to_ticker:
            return self._to_ticker[symbol]
        suffix = self.spec.symbol_suffix.upper()
        if suffix and symbol.endswith(suffix):
            return symbol[: -len(suffix)]
        # Fall back to stripping any exchange qualifier the vendor appended.
        for sep in (".", ":"):
            if sep in symbol:
                return symbol.split(sep)[0]
        return symbol


# ---------------------------------------------------------------------------
# Value extraction
# ---------------------------------------------------------------------------
def dig(row: Any, path: str) -> Any:
    """Read a dotted path out of nested JSON. Missing means None, never 0."""
    if not path:
        return row
    current = row
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return None
            current = current[int(part)]
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def as_float(value: Any) -> float | None:
    """Coerce a vendor value to a float, or None.

    Vendors send numbers as strings, send "NA"/"N/A"/"" for missing, and
    occasionally send a literal null. All of those mean *unknown*, and unknown
    must not become zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)  # drop NaN
    text = str(value).strip().replace(",", "")
    if text.lower() in ("", "na", "n/a", "null", "none", "-", "--"):
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return None if parsed != parsed else parsed


def as_time(value: Any, time_format: str) -> datetime | None:
    """Parse a vendor timestamp. An unparseable time is None, not now()."""
    if value is None or time_format == "none":
        return None
    if time_format == "epoch":
        seconds = as_float(value)
        if seconds is None or seconds <= 0:
            return None
        # Vendors send seconds or milliseconds; disambiguate by magnitude.
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rows(payload: Any, spec: VendorSpec) -> list[dict[str, Any]]:
    """Normalise a response into a list of row dicts.

    Vendors return a bare object for one symbol, a list for several, and a dict
    keyed by symbol for some batch endpoints. All three are handled.
    """
    root = dig(payload, spec.root) if spec.root else payload
    if root is None:
        return []
    if isinstance(root, list):
        return [r for r in root if isinstance(r, dict)]
    if isinstance(root, dict):
        # A dict of symbol -> row (Twelve Data's batch shape) versus a single row.
        values = list(root.values())
        looks_keyed = bool(values) and all(isinstance(v, dict) for v in values)
        if looks_keyed and spec.symbol_key not in root:
            out = []
            for key, value in root.items():
                row = dict(value)
                row.setdefault(spec.symbol_key, key)
                out.append(row)
            return out
        return [root]
    return []


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------
class RestQuoteProvider:
    """Calls a licensed vendor's REST endpoint and returns validated quotes.

    Not registered as a :class:`QuoteProvider` subclass here to avoid a circular
    import; :class:`~backend.market.quotes.LicensedQuoteProvider` wraps it and
    is the class the rest of the application sees.
    """

    def __init__(
        self,
        spec: VendorSpec,
        api_key: str,
        *,
        fetcher: HttpFetcher | None = None,
        mapper: SymbolMapper | None = None,
        delayed_minutes: int | None = None,
    ) -> None:
        self.spec = spec
        self._key = api_key
        self._mapper = mapper or SymbolMapper.load(spec)
        settings = get_settings()
        self.delayed_minutes = (
            delayed_minutes if delayed_minutes is not None
            else settings.quote_delay_minutes
        )
        headers = {}
        if spec.auth == "header" and api_key:
            headers[spec.auth_header] = spec.auth_header_format.format(key=api_key)
        self._fetcher = fetcher or HttpFetcher(headers=headers)

    # -- request building ---------------------------------------------------
    def _batches(self, symbols: Sequence[str]) -> Iterable[list[str]]:
        size = max(1, self.spec.max_batch) if self.spec.batch else 1
        for i in range(0, len(symbols), size):
            yield list(symbols[i : i + size])

    def _url_and_params(self, batch: list[str]) -> tuple[str, dict[str, str]]:
        joined = self.spec.batch_separator.join(batch)
        url = self.spec.url.format(
            symbol=joined, symbols=joined, key=self._key,
        )
        params: dict[str, str] = dict(self.spec.params)
        if self.spec.auth == "query" and self._key:
            params[self.spec.auth_param] = self._key
        # A batch endpoint whose template has no {symbol}/{symbols} placeholder
        # takes the symbols as a query parameter instead.
        if "{symbol" not in self.spec.url:
            params["symbol"] = joined
        return url, params

    # -- row -> quote -------------------------------------------------------
    def _quote_from_row(self, row: dict[str, Any], ticker: str) -> dict[str, Any] | None:
        """Map one vendor row. Returns None when the row is not usable.

        Refusing a row is the correct outcome far more often than it looks:
        a suspended stock, an unlisted symbol, a plan that does not cover this
        exchange, and a typo in the symbol map all arrive as a row with no
        price. None of them should reach a user as a number.
        """
        fields = self.spec.fields
        price = as_float(dig(row, fields["price"]))
        if price is None or price <= 0:
            return None

        currency = None
        if "currency" in fields:
            raw_currency = dig(row, fields["currency"])
            currency = str(raw_currency).strip().upper() if raw_currency else None
            if (
                self.spec.expect_currency
                and currency
                and currency != self.spec.expect_currency.upper()
            ):
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"Dropped {ticker}: vendor quoted {currency}, expected "
                    f"{self.spec.expect_currency}",
                    provider=self.spec.name,
                )
                return None

        values: dict[str, Any] = {"price": price}
        for name in ("previous_close", "open", "day_high", "day_low", "volume",
                     "turnover", "trades", "week52_high", "week52_low", "market_cap"):
            if name in fields:
                values[name] = as_float(dig(row, fields[name]))
        if values.get("trades") is not None:
            values["trades"] = int(values["trades"])

        # A non-positive previous close would make change% infinite or negative
        # nonsense; treat it as unknown rather than dividing by it.
        if values.get("previous_close") is not None and values["previous_close"] <= 0:
            values["previous_close"] = None

        values["currency"] = currency or self.spec.expect_currency or "EGP"
        if "quote_time" in fields:
            values["quote_time"] = as_time(dig(row, fields["quote_time"]), self.spec.time_format)
        return values

    # -- public API ---------------------------------------------------------
    def fetch(self, tickers: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return {ticker: field dict} for the tickers the vendor covered.

        Tickers the vendor did not cover are simply absent. They are never
        filled in from another source, and never estimated.
        """
        wanted = [t.upper() for t in tickers if t]
        if not wanted:
            return {}
        by_symbol = {self._mapper.to_vendor(t): t for t in wanted}
        out: dict[str, dict[str, Any]] = {}

        for batch in self._batches(list(by_symbol)):
            url, params = self._url_and_params(batch)
            payload = self._fetcher.get_json(url, params=params)
            rows = _rows(payload, self.spec)
            if not rows:
                log_event(
                    logger, EVENT_PROVIDER_FAILURE,
                    f"No quote rows in response for {', '.join(batch)}",
                    provider=self.spec.name,
                )
                continue
            for row in rows:
                symbol = str(dig(row, self.spec.symbol_key) or "").strip()
                ticker = by_symbol.get(symbol) or by_symbol.get(symbol.upper())
                if ticker is None:
                    ticker = self._mapper.to_ticker(symbol)
                    if ticker not in wanted:
                        # A single-symbol response may omit the symbol entirely.
                        if len(batch) == 1 and not symbol:
                            ticker = by_symbol[batch[0]]
                        else:
                            continue
                values = self._quote_from_row(row, ticker)
                if values is not None:
                    out[ticker] = values
        return out

    def close(self) -> None:
        self._fetcher.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_vendor_spec(
    vendor: str | None = None, spec_path: str | None = None
) -> VendorSpec | None:
    """Resolve the configured vendor spec, or None when none is configured.

    Precedence: an explicit spec file always wins, because it is how you correct
    a preset that no longer matches your account without waiting for a release.
    """
    settings = get_settings()
    vendor = (vendor if vendor is not None else settings.market_data_vendor).strip().lower()
    spec_path = spec_path if spec_path is not None else settings.market_data_spec_path

    if spec_path:
        p = Path(spec_path)
        if not p.exists():
            raise ProviderUnavailable(f"Market-data spec file not found: {p}")
        try:
            raw = json.loads(p.read_text())
        except ValueError as exc:
            raise ProviderUnavailable(f"Market-data spec {p} is not valid JSON: {exc}") from exc
        return VendorSpec.from_dict(raw)

    if not vendor or vendor in ("none", "off"):
        return None
    if vendor not in PRESETS:
        raise ProviderUnavailable(
            f"Unknown market-data vendor {vendor!r}. Built-in presets: "
            f"{', '.join(sorted(PRESETS))}. For any other vendor, write a spec "
            f"file and set EGX_MARKET_DATA_SPEC_PATH."
        )
    return PRESETS[vendor]


def describe_configuration() -> dict[str, Any]:
    """What the admin data-source panel needs to explain the current state."""
    settings = get_settings()
    try:
        spec = load_vendor_spec()
        error = None
    except ProviderUnavailable as exc:
        spec, error = None, str(exc)
    return {
        "vendor": spec.display_name if spec else None,
        "vendor_key": spec.name if spec else None,
        "docs": spec.docs if spec else None,
        "has_key": bool(settings.market_data_api_key),
        "delayed_minutes": settings.quote_delay_minutes,
        "symbol_suffix": spec.symbol_suffix if spec else None,
        "presets": sorted(PRESETS),
        "error": error,
        "market_status": market_state().status.value,
    }
