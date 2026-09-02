"""The live market-data path, tested without a network or a paid key.

Every vendor response here is a recorded-shape fixture served through an httpx
mock transport, so these tests prove the mapping, the validation and the refusal
rules — the things that decide whether a real price or a wrong one reaches a
user — without depending on a vendor being up or a key being present.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.data.providers.base import ProviderUnavailable
from backend.data.providers.http_client import HttpFetcher
from backend.market import live_providers as LP
from backend.market.quotes import LicensedQuoteProvider, refresh_quotes


# ---------------------------------------------------------------------------
# Fixtures shaped like the vendors' documented responses
# ---------------------------------------------------------------------------
EODHD_BATCH = [
    {"code": "COMI.EGX", "timestamp": 1_756_800_000, "open": 61.80, "high": 62.90,
     "low": 61.40, "close": 62.25, "previousClose": 63.38, "volume": 659050},
    {"code": "HRHO.EGX", "timestamp": 1_756_800_000, "open": 19.10, "high": 19.44,
     "low": 18.95, "close": 19.30, "previousClose": 19.02, "volume": 1_204_880},
    # A suspended stock: the vendor sends the row with no price.
    {"code": "SUSP.EGX", "timestamp": 1_756_800_000, "open": None, "high": None,
     "low": None, "close": "NA", "previousClose": 4.10, "volume": 0},
]

TWELVE_SINGLE = {
    "symbol": "COMI:EGX", "name": "Commercial International Bank",
    "currency": "EGP", "timestamp": 1_756_800_000,
    "open": "61.80", "high": "62.90", "low": "61.40", "close": "62.25",
    "previous_close": "63.38", "volume": "659050",
    "fifty_two_week": {"high": "78.40", "low": "48.10"},
}


def transport_for(payload, *, capture: list | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def provider_with(spec, payload, *, capture=None, key="test-key", **kwargs):
    fetcher = HttpFetcher(transport=transport_for(payload, capture=capture),
                          max_retries=0, rate_limit_per_second=0)
    return LP.RestQuoteProvider(spec, key, fetcher=fetcher,
                                mapper=LP.SymbolMapper(spec, {}), **kwargs)


# ---------------------------------------------------------------------------
class TestValueCoercion:
    """A vendor's "missing" must never become a number."""

    @pytest.mark.parametrize("raw", [None, "", "NA", "N/A", "null", "-", "--", "abc", float("nan")])
    def test_unknown_values_are_none_not_zero(self, raw):
        assert LP.as_float(raw) is None

    @pytest.mark.parametrize("raw,expected", [(62.25, 62.25), ("62.25", 62.25),
                                              ("1,204,880", 1204880.0), (0, 0.0)])
    def test_real_values_parse(self, raw, expected):
        assert LP.as_float(raw) == expected

    def test_booleans_are_not_numbers(self):
        assert LP.as_float(True) is None

    def test_epoch_seconds_and_milliseconds_both_parse(self):
        a = LP.as_time(1_756_800_000, "epoch")
        b = LP.as_time(1_756_800_000_000, "epoch")
        assert a == b == datetime(2025, 9, 2, 8, 0, tzinfo=timezone.utc)

    def test_unparseable_time_is_none_never_now(self):
        assert LP.as_time("not a time", "iso") is None
        assert LP.as_time(0, "epoch") is None

    def test_naive_iso_time_is_assumed_utc(self):
        assert LP.as_time("2025-09-02T08:00:00", "iso").tzinfo is timezone.utc


class TestDig:
    def test_reads_nested_paths(self):
        assert LP.dig({"a": {"b": {"c": 7}}}, "a.b.c") == 7

    def test_missing_path_is_none(self):
        assert LP.dig({"a": {}}, "a.b.c") is None

    def test_list_index(self):
        assert LP.dig({"rows": [{"p": 1}, {"p": 2}]}, "rows.1.p") == 2

    def test_out_of_range_index_is_none(self):
        assert LP.dig({"rows": []}, "rows.0.p") is None


class TestSymbolMapper:
    def test_default_rule_appends_the_suffix(self):
        m = LP.SymbolMapper(LP.EODHD, {})
        assert m.to_vendor("comi") == "COMI.EGX"

    def test_override_wins(self):
        m = LP.SymbolMapper(LP.EODHD, {"COMI": "CIB.CA"})
        assert m.to_vendor("COMI") == "CIB.CA"
        assert m.to_ticker("CIB.CA") == "COMI"

    def test_round_trip_without_override(self):
        m = LP.SymbolMapper(LP.EODHD, {})
        assert m.to_ticker(m.to_vendor("SWDY")) == "SWDY"

    def test_unknown_qualifier_is_stripped(self):
        assert LP.SymbolMapper(LP.EODHD, {}).to_ticker("SWDY.CA") == "SWDY"

    def test_malformed_symbol_map_is_an_error_not_a_silent_empty_map(self, tmp_path):
        bad = tmp_path / "map.json"
        bad.write_text("{not json")
        with pytest.raises(ProviderUnavailable):
            LP.SymbolMapper.load(LP.EODHD, bad)


class TestEodhdMapping:
    def test_batch_response_maps_onto_quotes(self):
        got = provider_with(LP.EODHD, EODHD_BATCH).fetch(["COMI", "HRHO"])
        assert set(got) == {"COMI", "HRHO"}
        assert got["COMI"]["price"] == 62.25
        assert got["COMI"]["previous_close"] == 63.38
        assert got["COMI"]["volume"] == 659050
        assert got["COMI"]["quote_time"] == datetime(2025, 9, 2, 8, 0, tzinfo=timezone.utc)

    def test_a_row_without_a_price_is_dropped_not_zeroed(self):
        got = provider_with(LP.EODHD, EODHD_BATCH).fetch(["COMI", "SUSP"])
        assert "SUSP" not in got, "a suspended stock must be absent, not 0.00"

    def test_a_ticker_the_vendor_omits_is_absent(self):
        got = provider_with(LP.EODHD, EODHD_BATCH).fetch(["COMI", "NOSUCH"])
        assert "NOSUCH" not in got

    def test_the_key_is_sent_as_the_vendor_expects(self):
        capture: list[httpx.Request] = []
        provider_with(LP.EODHD, EODHD_BATCH, capture=capture).fetch(["COMI"])
        assert capture[0].url.params["api_token"] == "test-key"
        assert capture[0].url.params["fmt"] == "json"
        assert "COMI.EGX" in str(capture[0].url)

    def test_batching_respects_the_vendor_limit(self):
        capture: list[httpx.Request] = []
        spec = LP.VendorSpec.from_dict({**LP.EODHD.to_dict(), "max_batch": 2})
        provider_with(spec, EODHD_BATCH, capture=capture).fetch(["A", "B", "C", "D", "E"])
        assert len(capture) == 3  # 2 + 2 + 1


class TestTwelveDataMapping:
    def test_single_object_response_maps(self):
        got = provider_with(LP.TWELVE_DATA, TWELVE_SINGLE).fetch(["COMI"])
        assert got["COMI"]["price"] == 62.25
        assert got["COMI"]["week52_high"] == 78.40
        assert got["COMI"]["currency"] == "EGP"

    def test_keyed_batch_response_maps(self):
        payload = {"COMI:EGX": TWELVE_SINGLE,
                   "HRHO:EGX": {**TWELVE_SINGLE, "symbol": "HRHO:EGX", "close": "19.30"}}
        got = provider_with(LP.TWELVE_DATA, payload).fetch(["COMI", "HRHO"])
        assert got["COMI"]["price"] == 62.25
        assert got["HRHO"]["price"] == 19.30

    def test_a_foreign_currency_row_is_refused(self):
        payload = {**TWELVE_SINGLE, "currency": "USD"}
        got = provider_with(LP.TWELVE_DATA, payload).fetch(["COMI"])
        assert got == {}, "a USD price under an EGP label is a wrong number"

    def test_a_non_positive_previous_close_becomes_unknown(self):
        payload = {**TWELVE_SINGLE, "previous_close": "0"}
        got = provider_with(LP.TWELVE_DATA, payload).fetch(["COMI"])
        assert got["COMI"]["previous_close"] is None


class TestVendorSpecValidation:
    def test_a_spec_without_price_is_rejected(self):
        with pytest.raises(ValueError, match="must map 'price'"):
            LP.VendorSpec.from_dict({"name": "x", "url": "https://e", "fields": {"open": "o"}})

    def test_an_unknown_field_is_rejected_not_ignored(self):
        with pytest.raises(ValueError, match="unknown field"):
            LP.VendorSpec.from_dict({
                "name": "x", "url": "https://e",
                "fields": {"price": "p", "eps_ttm": "e"},
            })

    def test_an_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown key"):
            LP.VendorSpec.from_dict({"name": "x", "url": "https://e",
                                     "fields": {"price": "p"}, "typo": 1})

    def test_round_trips_through_json(self):
        again = LP.VendorSpec.from_dict(json.loads(json.dumps(LP.EODHD.to_dict())))
        assert again == LP.EODHD

    def test_a_custom_vendor_needs_no_code(self, tmp_path, monkeypatch):
        """The whole point of the spec layer: any vendor, no code change."""
        spec_file = tmp_path / "vendor.json"
        spec_file.write_text(json.dumps({
            "name": "housebroker", "display_name": "House Broker Feed",
            "url": "https://api.broker.example/v2/quotes",
            "auth": "header", "auth_header": "X-Api-Key", "auth_header_format": "{key}",
            "root": "data.quotes", "symbol_key": "ric",
            "fields": {"price": "last.px", "previous_close": "ref.close",
                       "volume": "last.qty", "quote_time": "last.ts"},
            "time_format": "iso", "symbol_suffix": ".CA",
        }))
        monkeypatch.setenv("EGX_MARKET_DATA_SPEC_PATH", str(spec_file))
        from backend.core.config import get_settings
        get_settings.cache_clear()
        try:
            spec = LP.load_vendor_spec()
            assert spec.name == "housebroker"
            payload = {"data": {"quotes": [
                {"ric": "COMI.CA", "last": {"px": 62.25, "qty": 659050,
                                            "ts": "2025-09-02T12:30:00Z"},
                 "ref": {"close": 63.38}},
            ]}}
            got = provider_with(spec, payload).fetch(["COMI"])
            assert got["COMI"]["price"] == 62.25
            assert got["COMI"]["quote_time"].hour == 12
        finally:
            get_settings.cache_clear()


class TestLicensedProviderGating:
    """Both halves of the configuration are required. Neither alone goes live."""

    def _settings(self, monkeypatch, **env):
        from backend.core.config import get_settings
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings

    def test_no_vendor_and_no_key_serves_nothing(self):
        p = LicensedQuoteProvider()
        assert not p.is_available()
        assert p.get_quotes(["COMI"]) == {}
        assert "no live feed" in (p.unavailable_reason() or "").lower()

    def test_a_key_without_a_vendor_is_a_misconfiguration_not_a_feed(self, monkeypatch):
        gs = self._settings(monkeypatch, EGX_MARKET_DATA_API_KEY="k")
        try:
            p = LicensedQuoteProvider()
            assert not p.is_available()
            assert "no vendor is named" in (p.unavailable_reason() or "")
            assert p.get_quotes(["COMI"]) == {}
        finally:
            gs.cache_clear()

    def test_a_vendor_without_a_key_is_a_misconfiguration(self, monkeypatch):
        gs = self._settings(monkeypatch, EGX_MARKET_DATA_VENDOR="eodhd")
        try:
            p = LicensedQuoteProvider()
            assert not p.is_available()
            assert "API_KEY is empty" in (p.unavailable_reason() or "")
        finally:
            gs.cache_clear()

    def test_an_unknown_vendor_name_is_reported_not_guessed(self, monkeypatch):
        gs = self._settings(monkeypatch, EGX_MARKET_DATA_VENDOR="nasdaq-totalview",
                            EGX_MARKET_DATA_API_KEY="k")
        try:
            p = LicensedQuoteProvider()
            assert not p.is_available()
            assert "Unknown market-data vendor" in (p.unavailable_reason() or "")
        finally:
            gs.cache_clear()

    def test_it_never_falls_back_to_demo_when_the_vendor_fails(self, monkeypatch, db):
        """The failure this whole architecture exists to prevent."""
        gs = self._settings(monkeypatch, EGX_MARKET_DATA_VENDOR="eodhd",
                            EGX_MARKET_DATA_API_KEY="k")
        try:
            fetcher = HttpFetcher(transport=transport_for({"error": "down"}, status=503),
                                  max_retries=0, rate_limit_per_second=0)
            client = LP.RestQuoteProvider(LP.EODHD, "k", fetcher=fetcher,
                                          mapper=LP.SymbolMapper(LP.EODHD, {}))
            provider = LicensedQuoteProvider(client=client)
            stored = refresh_quotes(db, ["COMI"], provider=provider)
            assert stored == {}, "an outage must produce nothing, never demo prices"
        finally:
            gs.cache_clear()


class TestLiveQuotesReachTheDatabase:
    """End to end: vendor JSON in, labelled live quote out."""

    def test_a_live_quote_is_stored_and_is_not_labelled_demo(self, db):
        client = provider_with(LP.EODHD, EODHD_BATCH)
        provider = LicensedQuoteProvider(client=client)
        stored = refresh_quotes(db, ["COMI", "HRHO"], provider=provider)

        assert set(stored) == {"COMI", "HRHO"}
        comi = stored["COMI"]
        assert comi.price == 62.25
        assert comi.is_demo is False
        assert comi.source == "eodhd"
        assert comi.change_pct == pytest.approx((62.25 - 63.38) / 63.38)

    def test_the_reported_delay_comes_from_the_licence_not_the_vendor(self, db, monkeypatch):
        from backend.core.config import get_settings
        monkeypatch.setenv("EGX_QUOTE_DELAY_MINUTES", "0")
        get_settings.cache_clear()
        try:
            client = provider_with(LP.EODHD, EODHD_BATCH)
            provider = LicensedQuoteProvider(client=client)
            stored = refresh_quotes(db, ["COMI"], provider=provider)
            assert stored["COMI"].delayed_minutes == 0
        finally:
            get_settings.cache_clear()

    def test_a_vendor_outage_leaves_the_previous_quote_untouched(self, db):
        ok = LicensedQuoteProvider(client=provider_with(LP.EODHD, EODHD_BATCH))
        refresh_quotes(db, ["COMI"], provider=ok)
        db.flush()

        fetcher = HttpFetcher(transport=transport_for({}, status=503),
                              max_retries=0, rate_limit_per_second=0)
        broken = LicensedQuoteProvider(
            client=LP.RestQuoteProvider(LP.EODHD, "k", fetcher=fetcher,
                                        mapper=LP.SymbolMapper(LP.EODHD, {}))
        )
        refresh_quotes(db, ["COMI"], provider=broken)

        from backend.data.saas_models import Quote
        assert db.get(Quote, "COMI").price == 62.25, (
            "a failed refresh must not blank or invent a price"
        )
