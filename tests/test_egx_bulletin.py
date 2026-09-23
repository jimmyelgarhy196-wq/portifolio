"""The EGX bulletin importer.

A bulletin is exactly the kind of file where a quiet column mis-match puts one
company's price under another company's name, so these tests are mostly about
what the importer *refuses* to do.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from backend.data.providers.base import ProviderUnavailable
from backend.data.providers import egx_bulletin as B

ENGLISH_CSV = (
    "Egyptian Exchange — Daily Trading Bulletin\n"
    "Session date: 22/09/2026\n"
    "\n"
    "Reuters Code,Company Name,Opening Price,Highest Price,Lowest Price,Closing Price,"
    "Previous Close,Traded Volume,Traded Value,No. of Trades\n"
    "COMI.CA,Commercial International Bank,61.80,62.90,61.40,62.25,63.38,659050,41062762.50,3421\n"
    "HRHO.CA,EFG Hermes Holding,19.10,19.44,18.95,19.30,19.02,\"1,204,880\",23254184,1877\n"
    "SUSP.CA,Suspended Co,-,-,-,-,4.10,0,0,0\n"
    "SWDY.CA,Elsewedy Electric,18.20,18.44,18.10,18.24,18.24,\"1,428,837\",26062  ,900\n"
)

ARABIC_CSV = (
    "﻿الكود,اسم الشركة,سعر الافتتاح,أعلى سعر,أدنى سعر,سعر الإغلاق,حجم التداول\n"
    "COMI,البنك التجاري الدولي,٦١٫٨٠,62.90,61.40,62.25,659050\n"
    "ETEL,المصرية للاتصالات,34.10,34.40,33.90,34.06,2268282\n"
)


@pytest.fixture
def egx_universe(db):
    """The handful of covered issuers the bulletin rows refer to."""
    from backend.data import models

    for ticker, name in [("COMI", "Commercial International Bank"),
                         ("HRHO", "EFG Hermes Holding"),
                         ("SWDY", "Elsewedy Electric"),
                         ("ETEL", "Telecom Egypt")]:
        db.add(models.Company(ticker=ticker, name=name, sector="Banks",
                              exchange="EGX", currency="EGP", status="ACTIVE"))
    db.flush()


def parse(text: str, **kw):
    return B.parse_bulletin(text.encode("utf-8"), filename=kw.pop("filename", "b.csv"), **kw)


class TestHeaderNormalisation:
    @pytest.mark.parametrize("a,b", [
        ("أعلى سعر", "اعلى سعر"),
        ("سعر الإغلاق", "سعر الاغلاق"),
        ("Closing  Price", "closing price"),
        ("No. of Trades", "no of trades"),
        ("حجم  التداول", "حجم التداول"),
    ])
    def test_variants_fold_together(self, a, b):
        assert B.normalise_header(a) == B.normalise_header(b)

    def test_tatweel_padding_is_ignored(self):
        assert B.normalise_header("الكـــود") == B.normalise_header("الكود")


class TestNumberParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("62.25", 62.25), ("1,204,880", 1204880.0), ("٦١٫٨٠", 61.80),
        ("(3.5)", -3.5), ("  18.24  ", 18.24), ("EGP 62.25", 62.25),
    ])
    def test_real_numbers(self, raw, expected):
        assert B.clean_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "-", "--", "N/A", "لا يوجد", None, "abc"])
    def test_missing_is_none_never_zero(self, raw):
        assert B.clean_number(raw) is None


class TestTickerCleaning:
    @pytest.mark.parametrize("raw,expected", [
        ("COMI.CA", "COMI"), ("comi", "COMI"), ("HRHO.EGX", "HRHO"),
        ("SWDY-CA", "SWDY"), ("ETEL  Egypt", "ETEL"),
    ])
    def test_suffixes_stripped(self, raw, expected):
        assert B.clean_ticker(raw) == expected

    def test_empty_is_none(self):
        assert B.clean_ticker("  ") is None


class TestParsing:
    def test_header_row_is_found_below_title_rows(self):
        result = parse(ENGLISH_CSV)
        assert result.mapping["close"] == "Closing Price"
        assert result.mapping["ticker"] == "Reuters Code"

    def test_rows_parse_with_thousands_separators(self):
        result = parse(ENGLISH_CSV)
        by_ticker = {r.ticker: r for r in result.rows}
        assert by_ticker["COMI"].close == 62.25
        assert by_ticker["HRHO"].volume == 1204880.0

    def test_a_row_with_no_close_is_rejected_with_a_reason(self):
        result = parse(ENGLISH_CSV)
        assert "SUSP" not in {r.ticker for r in result.rows}
        assert ("SUSP", "no closing price") in result.rejected

    def test_arabic_headers_and_digits(self):
        result = parse(ARABIC_CSV, filename="نشرة.csv")
        assert result.mapping["close"] == "سعر الإغلاق"
        by_ticker = {r.ticker: r for r in result.rows}
        assert by_ticker["COMI"].open == pytest.approx(61.80)
        assert by_ticker["ETEL"].close == 34.06

    def test_high_below_low_is_rejected_as_incoherent(self):
        csv_text = ("Code,Closing Price,Highest Price,Lowest Price\n"
                    "AAAA,10.0,9.0,11.0\n")
        result = parse(csv_text)
        assert result.rows == []
        assert result.rejected[0][1].startswith("high 9.0 below low 11.0")

    def test_semicolon_delimited_file(self):
        result = parse("Code;Closing Price\nCOMI;62.25\n")
        assert result.rows[0].close == 62.25

    def test_unmapped_columns_are_reported_not_silently_dropped(self):
        result = parse("Code,Closing Price,Board Lot\nCOMI,62.25,100\n")
        assert "Board Lot" in result.unmapped_headers


class TestRefusals:
    def test_a_file_without_a_close_column_is_refused_whole(self):
        with pytest.raises(ProviderUnavailable, match="close"):
            parse("Code,Company\nCOMI,CIB\n")

    def test_the_error_lists_the_headers_it_found(self):
        with pytest.raises(ProviderUnavailable) as exc:
            parse("Code,Company,Board Lot\nCOMI,CIB,100\n")
        assert "Board Lot" in str(exc.value)

    def test_a_file_with_no_recognisable_header_row_is_refused(self):
        with pytest.raises(ProviderUnavailable, match="header row"):
            parse("alpha,beta\n1,2\n")

    def test_an_override_naming_a_missing_column_is_an_error(self):
        with pytest.raises(ProviderUnavailable, match="not"):
            parse(ENGLISH_CSV, overrides={"close": "No Such Column"})

    def test_an_override_naming_an_unknown_field_is_an_error(self):
        with pytest.raises(ProviderUnavailable, match="unknown field"):
            parse(ENGLISH_CSV, overrides={"eps": "Closing Price"})

    def test_an_override_beats_the_alias_match(self):
        result = parse(ENGLISH_CSV, overrides={"close": "Previous Close"})
        assert result.mapping["close"] == "Previous Close"
        assert {r.ticker: r.close for r in result.rows}["COMI"] == 63.38


class TestBulletinDate:
    def test_explicit_date_wins(self):
        result = parse(ENGLISH_CSV, bulletin_date=date(2026, 9, 22))
        assert result.bulletin_date == date(2026, 9, 22)
        assert result.date_origin == "argument"

    def test_date_is_read_from_a_date_column(self):
        result = parse("Code,Closing Price,Trade Date\nCOMI,62.25,2026-09-22\n")
        assert result.bulletin_date == date(2026, 9, 22)
        assert "column" in result.date_origin

    def test_date_falls_back_to_the_filename(self):
        result = parse(ENGLISH_CSV, filename="EGX_2026-09-22.csv")
        assert result.bulletin_date == date(2026, 9, 22)
        assert result.date_origin == "filename"

    def test_an_undated_bulletin_is_never_stamped_today(self):
        result = parse(ENGLISH_CSV, filename="bulletin.csv")
        assert result.bulletin_date is None


class TestIngestion:
    def test_rows_are_stored_with_egx_provenance(self, db, egx_universe):
        from backend.data.models import PriceBar

        result = parse(ENGLISH_CSV, bulletin_date=date(2026, 9, 22))
        counts = B.ingest_bulletin(db, result)
        assert counts["inserted"] >= 1

        bar = db.query(PriceBar).filter_by(ticker="COMI", timestamp=date(2026, 9, 22)).one()
        assert bar.close == 62.25
        assert bar.source.startswith("EGX:bulletin")
        assert bar.data_period == "2026-09-22"
        assert bar.retrieved_at is not None

    def test_reimporting_the_same_day_updates_rather_than_duplicates(self, db, egx_universe):
        from backend.data.models import PriceBar

        result = parse(ENGLISH_CSV, bulletin_date=date(2026, 9, 22))
        B.ingest_bulletin(db, result)
        second = B.ingest_bulletin(db, result)
        assert second["inserted"] == 0 and second["updated"] >= 1
        assert db.query(PriceBar).filter_by(
            ticker="COMI", timestamp=date(2026, 9, 22)).count() == 1

    def test_a_code_outside_the_universe_is_counted_not_invented(self, db, egx_universe):
        result = parse("Code,Closing Price\nZZZZ,5.00\n", bulletin_date=date(2026, 9, 22))
        counts = B.ingest_bulletin(db, result)
        assert counts["unknown_ticker"] == 1 and counts["inserted"] == 0

    def test_an_undated_bulletin_is_refused_at_ingest(self, db):
        result = parse(ENGLISH_CSV, filename="bulletin.csv")
        with pytest.raises(ProviderUnavailable, match="no date"):
            B.ingest_bulletin(db, result)


class TestExcel:
    def test_an_xlsx_bulletin_reads(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "EGX_2026-09-22.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Egyptian Exchange Daily Bulletin"])
        ws.append([])
        ws.append(["Reuters Code", "Closing Price", "Traded Volume"])
        ws.append(["COMI.CA", 62.25, 659050])
        ws.append(["HRHO.CA", 19.30, 1204880])
        wb.save(path)

        result = B.parse_bulletin(path)
        assert len(result.rows) == 2
        assert result.bulletin_date == date(2026, 9, 22)
        assert result.rows[0].close == 62.25

    def test_legacy_xls_is_refused_with_advice(self, tmp_path):
        path = tmp_path / "old.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        with pytest.raises(ProviderUnavailable, match="save as"):
            B.parse_bulletin(path)
