"""Ingest the Egyptian Exchange's own published end-of-day bulletin.

EGX publishes daily trading data on egx.com.eg as a downloadable file. This
reads that file. It is the *primary* source — the exchange itself — which makes
it the most defensible input this platform can have, and the one whose terms you
negotiate with EGX rather than with a reseller.

**The format is data, not code.** EGX has changed its site and its file layout
more than once, publishes in Arabic and in English, and ships CSV in some places
and Excel in others. So nothing here hard-codes a column position. Headers are
matched by alias, the match is *reported*, and anything the matcher gets wrong is
corrected in a JSON file rather than in Python:

    {"close": "Closing Price", "ticker": "Reuters Code"}

The honesty rules are the same as everywhere else in this codebase, because a
bulletin is exactly the kind of file where a quiet column mis-match would put
one company's price under another company's name:

* If the required columns cannot be matched, the **whole file is refused** and
  the error lists every header found. A bulletin is never read by column
  position — position is how you silently import the wrong column.
* A row with no usable positive close is **rejected and counted**, with a
  reason, never defaulted to zero.
* The bulletin's date comes from an explicit argument, a date column, or the
  filename — in that order. It is never silently assumed to be today, because a
  file downloaded on Sunday covering Thursday would then be stamped with a date
  on which nothing traded.
* Every row carries ``source``, ``retrieved_at`` and ``data_period`` so the
  provenance panel can show where the number came from.

This reads a file EGX publishes for download. It is not a scraper of a vendor
terminal, and it does not bypass anyone's paywall. Redistributing EGX data to
paying subscribers is still a licensing question for EGX — see
``docs/GOING_LIVE.md``.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from backend.core.logging_config import get_logger
from backend.data.providers.base import ProviderUnavailable
from backend.data.providers.csv_provider import parse_date

logger = get_logger(__name__)

#: Fields a bulletin may carry. "ticker" and "close" are required; a file
#: without both is not a price bulletin and is refused rather than guessed at.
REQUIRED_FIELDS = ("ticker", "close")

#: field -> header aliases, English and Arabic. Matching is accent-, case- and
#: whitespace-insensitive (see :func:`normalise_header`). These are a
#: convenience: an alias that does not fit your file costs nothing, because an
#: unmatched column is reported rather than guessed, and overridden in JSON.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "ticker": (
        "ticker", "symbol", "code", "stock code", "reuters code", "reuters",
        "trading code", "security code", "isin",
        "الكود", "الرمز", "رمز", "كود", "رمز الشركة", "كود الشركة",
        "الكود المختصر", "رمز التداول",
    ),
    "name": (
        "name", "company", "company name", "security name", "stock name",
        "اسم الشركة", "الشركة", "اسم الورقة", "اسم الورقة المالية",
    ),
    "open": (
        "open", "opening price", "open price", "first price",
        "سعر الافتتاح", "الافتتاح", "سعر الفتح", "اول سعر",
    ),
    "high": (
        "high", "highest price", "high price", "max price", "day high",
        "اعلى سعر", "الاعلى", "اعلي سعر", "اعلى",
    ),
    "low": (
        "low", "lowest price", "low price", "min price", "day low",
        "ادنى سعر", "الادنى", "اقل سعر", "ادني سعر", "ادنى",
    ),
    "close": (
        "close", "closing price", "close price", "last", "last price",
        "last traded price", "ltp", "today closing price",
        "سعر الاغلاق", "الاغلاق", "سعر الإغلاق", "اخر سعر", "آخر سعر",
        "سعر اخر تنفيذ",
    ),
    "previous_close": (
        "previous close", "prev close", "previous closing price",
        "reference price", "yesterday close",
        "سعر الاغلاق السابق", "الاغلاق السابق", "السعر المرجعي",
        "اغلاق الامس",
    ),
    "volume": (
        "volume", "traded volume", "quantity", "traded quantity", "shares",
        "no of shares", "number of shares", "volume traded",
        "حجم التداول", "الكمية", "كمية التداول", "عدد الاسهم", "عدد الأسهم",
    ),
    "turnover": (
        "turnover", "value", "traded value", "value traded", "turnover egp",
        "قيمة التداول", "القيمة", "قيمة",
    ),
    "trades": (
        "trades", "no of trades", "number of trades", "transactions", "deals",
        "عدد العمليات", "عدد الصفقات", "العمليات", "الصفقات",
    ),
    "date": (
        "date", "trade date", "trading date", "session date", "as of",
        "التاريخ", "تاريخ الجلسة", "تاريخ التداول", "تاريخ",
    ),
}

_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_ALNUM = re.compile(r"[^0-9a-z؀-ۿ]+")
#: Exchange qualifiers vendors and bulletins append to an EGX ticker.
_TICKER_SUFFIX = re.compile(r"[.\-:](CA|EGX|EG|CAI)$", re.IGNORECASE)


def normalise_header(text: Any) -> str:
    """Fold a header to a comparable key.

    Arabic text in published files varies in ways that are invisible on screen:
    alef spelled أ / إ / آ / ا, teh marbuta ة versus ه, tatweel padding for
    justification, and diacritics. All of them must compare equal or an Arabic
    bulletin will look unmatched for no reason a user can see.
    """
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = _ARABIC_DIACRITICS.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي").replace("ﻻ", "لا")
    s = _NON_ALNUM.sub(" ", s)
    return " ".join(s.split())


_ALIAS_INDEX: dict[str, str] = {}
for _field, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_INDEX.setdefault(normalise_header(_alias), _field)


def clean_number(value: Any) -> float | None:
    """Parse a number out of a bulletin cell, or return None.

    Published files carry thousands separators, currency words, parenthesised
    negatives, Arabic-Indic digits, and dashes for "no trade". Every one of
    those means a value or means unknown — none of them mean zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    # Arabic-Indic and Eastern Arabic-Indic digits, then the Arabic decimal
    # separator. Stripping ٫ instead of converting it turns 61٫80 into 6180 —
    # a hundredfold error that still looks like a plausible price.
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    text = text.replace("\u066b", ".").replace("\u066c", "")
    if text.lower() in ("-", "--", "n/a", "na", "nil", "none", "null", "لا يوجد", "-"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace(",", "").replace("٬", "").replace("،", "").replace(" ", "")
    text = re.sub(r"[^\d.\-+]", "", text)
    if text in ("", "-", "+", "."):
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed != parsed:
        return None
    return -parsed if negative else parsed


def clean_ticker(value: Any) -> str | None:
    """Normalise a bulletin's security code to a GMG ticker."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    text = text.split()[0] if text else ""
    text = _TICKER_SUFFIX.sub("", text)
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text or None


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------
def read_table(source: str | Path | bytes, *, filename: str = "") -> tuple[list[str], list[dict[str, Any]]]:
    """Read a CSV or Excel bulletin into (headers, rows).

    The header row is not assumed to be the first row: published bulletins
    routinely carry a title, a logo row and a blank line above the real headers.
    The first row that matches at least two known field aliases is the header.
    """
    name = filename or (str(source) if isinstance(source, (str, Path)) else "")
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    suffix = Path(name).suffix.lower()

    if suffix in (".xlsx", ".xlsm", ".xls"):
        grid = _read_excel(raw, suffix)
    else:
        grid = _read_delimited(raw)

    if not grid:
        raise ProviderUnavailable(f"{name or 'bulletin'} contains no rows.")

    header_idx = _find_header_row(grid)
    if header_idx is None:
        preview = " | ".join(str(c) for c in grid[0][:12])
        raise ProviderUnavailable(
            f"Could not find a header row in {name or 'the bulletin'}. No row "
            f"matched two or more known columns. First row was: {preview}"
        )

    headers = [str(c).strip() for c in grid[header_idx]]
    rows: list[dict[str, Any]] = []
    for raw_row in grid[header_idx + 1:]:
        if not any(str(c).strip() for c in raw_row):
            continue
        row = {headers[i]: raw_row[i] for i in range(min(len(headers), len(raw_row)))}
        rows.append(row)
    return headers, rows


def _read_delimited(raw: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 never raises
        text = raw.decode("utf-8", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _read_excel(raw: bytes, suffix: str) -> list[list[Any]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProviderUnavailable(
            "Reading an Excel bulletin needs openpyxl: pip install openpyxl"
        ) from exc
    if suffix == ".xls":
        raise ProviderUnavailable(
            "Legacy .xls is not supported. Open it and save as .xlsx or .csv."
        )
    try:
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises several types
        raise ProviderUnavailable(
            f"Could not open this as an Excel workbook ({exc.__class__.__name__}). "
            "If the file is really a CSV, give it a .csv extension."
        ) from exc
    sheet = book[book.sheetnames[0]]
    return [list(r) for r in sheet.iter_rows(values_only=True)]


def _find_header_row(grid: list[list[Any]], *, scan: int = 25) -> int | None:
    best_idx, best_hits = None, 0
    for idx, row in enumerate(grid[:scan]):
        hits = sum(1 for cell in row if normalise_header(cell) in _ALIAS_INDEX)
        if hits > best_hits:
            best_idx, best_hits = idx, hits
    return best_idx if best_hits >= 2 else None


# ---------------------------------------------------------------------------
# Mapping headers onto fields
# ---------------------------------------------------------------------------
def detect_mapping(
    headers: Sequence[str], overrides: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Return (field -> header, unmapped headers).

    An explicit override always wins, and an override naming a header that is
    not in the file is an error rather than a silent no-op — a typo in a mapping
    file must not look like a successful import.
    """
    mapping: dict[str, str] = {}
    by_key = {normalise_header(h): h for h in headers if str(h).strip()}

    for field_name, header in (overrides or {}).items():
        if field_name not in FIELD_ALIASES:
            raise ProviderUnavailable(
                f"Mapping refers to unknown field {field_name!r}. "
                f"Known fields: {', '.join(sorted(FIELD_ALIASES))}."
            )
        key = normalise_header(header)
        if key not in by_key:
            raise ProviderUnavailable(
                f"Mapping sends {field_name!r} to column {header!r}, which is not "
                f"in the file. Columns present: {', '.join(headers)}"
            )
        mapping[field_name] = by_key[key]

    for key, header in by_key.items():
        field_name = _ALIAS_INDEX.get(key)
        if field_name and field_name not in mapping:
            mapping[field_name] = header

    mapped = set(mapping.values())
    unmapped = [h for h in headers if str(h).strip() and h not in mapped]
    return mapping, unmapped


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class BulletinRow:
    ticker: str
    close: float
    name: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    turnover: float | None = None
    trades: int | None = None


@dataclass
class BulletinParseResult:
    """What a file yielded, and everything it refused, with reasons."""

    rows: list[BulletinRow] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped_headers: list[str] = field(default_factory=list)
    bulletin_date: date | None = None
    source: str = "EGX:bulletin"
    date_origin: str = "unknown"

    @property
    def ok(self) -> int:
        return len(self.rows)

    def summary(self) -> str:
        return (
            f"{self.ok} row(s) parsed, {len(self.rejected)} rejected, "
            f"date {self.bulletin_date or 'unknown'} (from {self.date_origin})"
        )


_DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def date_from_filename(name: str) -> date | None:
    m = _DATE_IN_NAME.search(Path(name).stem)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_bulletin(
    source: str | Path | bytes,
    *,
    filename: str = "",
    overrides: dict[str, str] | None = None,
    bulletin_date: date | None = None,
    source_label: str | None = None,
) -> BulletinParseResult:
    """Parse an EGX bulletin. Refuses the file rather than guessing at it."""
    name = filename or (str(source) if isinstance(source, (str, Path)) else "bulletin")
    headers, raw_rows = read_table(source, filename=name)
    mapping, unmapped = detect_mapping(headers, overrides)

    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise ProviderUnavailable(
            f"{Path(name).name}: could not identify the {', '.join(missing)} "
            f"column(s). Columns found: {', '.join(headers)}. "
            f"Map them explicitly with --map, e.g. "
            f"{{\"close\": \"<the closing price column>\"}}. The file is not "
            f"read by column position, because that silently imports the wrong "
            f"column when the layout changes."
        )

    result = BulletinParseResult(
        mapping=mapping, unmapped_headers=unmapped,
        source=source_label or f"EGX:bulletin:{Path(name).name}",
    )

    # Bulletin date: explicit, then a date column, then the filename. Never now().
    if bulletin_date is not None:
        result.bulletin_date, result.date_origin = bulletin_date, "argument"
    elif "date" in mapping:
        for row in raw_rows:
            parsed = parse_date(row.get(mapping["date"]))
            if parsed:
                result.bulletin_date, result.date_origin = parsed, f"column {mapping['date']!r}"
                break
    if result.bulletin_date is None:
        from_name = date_from_filename(name)
        if from_name:
            result.bulletin_date, result.date_origin = from_name, "filename"

    for index, row in enumerate(raw_rows, start=1):
        ticker = clean_ticker(row.get(mapping["ticker"]))
        label = ticker or f"row {index}"
        if not ticker:
            result.rejected.append((label, "no security code"))
            continue
        close = clean_number(row.get(mapping["close"]))
        if close is None:
            result.rejected.append((ticker, "no closing price"))
            continue
        if close <= 0:
            result.rejected.append((ticker, f"non-positive close ({close})"))
            continue

        def pick(field_name: str) -> float | None:
            return clean_number(row.get(mapping[field_name])) if field_name in mapping else None

        high, low = pick("high"), pick("low")
        if high is not None and low is not None and high < low:
            result.rejected.append((ticker, f"high {high} below low {low}"))
            continue

        trades = pick("trades")
        result.rows.append(BulletinRow(
            ticker=ticker, close=close,
            name=(str(row.get(mapping["name"])).strip() if "name" in mapping else None) or None,
            open=pick("open"), high=high, low=low,
            previous_close=pick("previous_close"),
            volume=pick("volume"), turnover=pick("turnover"),
            trades=int(trades) if trades is not None else None,
        ))

    return result


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------
def ingest_bulletin(
    session: Any, result: BulletinParseResult, *, only_known_tickers: bool = True
) -> dict[str, int]:
    """Upsert parsed bulletin rows as daily price bars.

    ``only_known_tickers`` keeps the universe authoritative: a code the platform
    does not cover is counted and skipped rather than silently creating a
    company row from a spreadsheet cell.
    """
    from sqlalchemy import select

    from backend.data.models import Company, PriceBar

    if result.bulletin_date is None:
        raise ProviderUnavailable(
            "The bulletin has no date. Pass --date YYYY-MM-DD. A price bar "
            "without a true trading date is worse than no price bar."
        )

    known = {
        t for (t,) in session.execute(select(Company.ticker)).all()
    } if only_known_tickers else None

    counts = {"inserted": 0, "updated": 0, "unknown_ticker": 0}
    retrieved = datetime.now(timezone.utc).replace(tzinfo=None)

    for row in result.rows:
        if known is not None and row.ticker not in known:
            counts["unknown_ticker"] += 1
            continue
        existing = session.scalar(
            select(PriceBar).where(
                PriceBar.ticker == row.ticker,
                PriceBar.timestamp == result.bulletin_date,
            )
        )
        payload = {
            "open": row.open, "high": row.high, "low": row.low,
            "close": row.close, "adjusted_close": row.close,
            "volume": row.volume,
            "source": result.source,
            "retrieved_at": retrieved,
            "data_period": result.bulletin_date.isoformat(),
            "confidence": "HIGH",
        }
        if existing is None:
            session.add(PriceBar(
                ticker=row.ticker, timestamp=result.bulletin_date, **payload
            ))
            counts["inserted"] += 1
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
            counts["updated"] += 1

    session.flush()
    return counts
