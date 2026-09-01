"""The report Markdown renderer.

Reports quote headlines, disclosure titles and provider names that came from
outside this system, so the renderer escapes before it formats. These tests pin
that ordering: no path may emit markup that came from the source text.
"""
from __future__ import annotations

import pytest

from backend.api.markdown_render import render_markdown


class TestStructure:
    def test_empty_input_renders_nothing(self):
        assert render_markdown("") == ""
        assert render_markdown(None) == ""

    def test_headings_shift_down_one_level(self):
        """The page already has an h1, so a document h1 becomes an h2."""
        assert "<h2>Title</h2>" in render_markdown("# Title")
        assert "<h3>Section</h3>" in render_markdown("## Section")

    def test_paragraphs_join_wrapped_lines(self):
        out = render_markdown("One line\ncontinued here.\n\nSecond paragraph.")
        assert "<p>One line continued here.</p>" in out
        assert "<p>Second paragraph.</p>" in out

    def test_bullet_and_numbered_lists(self):
        assert render_markdown("- a\n- b").count("<li>") == 2
        out = render_markdown("1. first\n2. second")
        assert out.startswith("<ol>") and out.count("<li>") == 2

    def test_horizontal_rule(self):
        assert "<hr>" in render_markdown("---")

    def test_blockquote_lines_merge_into_one_callout(self):
        out = render_markdown("> **Warning**\n> Second line.")
        assert out.count("<blockquote") == 1, "a quote must not fragment per line"
        assert "Second line." in out

    def test_inline_formatting(self):
        out = render_markdown("**bold** and *italic* and `code`")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out
        assert "<code>code</code>" in out

    def test_unrecognised_input_becomes_a_paragraph(self):
        assert render_markdown("just text") == "<p>just text</p>"


class TestTables:
    TABLE = (
        "| Ticker | Move | Note |\n"
        "|---|---:|---|\n"
        "| **AMOC** | +10.18% | Board meeting |\n"
        "| JUFO | -8.36% | Another |\n"
    )

    def test_table_renders_as_a_table(self):
        out = render_markdown(self.TABLE)
        assert "<table" in out and out.count("<tr>") == 3   # header + 2 rows
        assert "<th>Ticker</th>" in out

    def test_numeric_columns_are_right_aligned(self):
        out = render_markdown(self.TABLE)
        assert '<th class="num">Move</th>' in out
        assert '<td class="num">+10.18%</td>' in out
        # A text column is not.
        assert "<td>Board meeting</td>" in out

    def test_table_scrolls_rather_than_widening_the_page(self):
        assert 'class="tbl-scroll"' in render_markdown(self.TABLE)

    def test_cell_formatting_is_applied(self):
        assert "<strong>AMOC</strong>" in render_markdown(self.TABLE)


class TestEscaping:
    """Escaping happens before formatting, so injected markup cannot survive."""

    @pytest.mark.parametrize("payload", [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src='evil'></iframe>",
        "<svg onload=alert(1)>",
        "</p><script>alert(1)</script><p>",
    ])
    def test_html_in_the_source_is_escaped(self, payload):
        out = render_markdown(f"A headline: {payload}")
        assert "<script" not in out
        assert "<img" not in out
        assert "<iframe" not in out
        assert "<svg" not in out
        assert "&lt;" in out

    def test_html_inside_a_table_cell_is_escaped(self):
        out = render_markdown(
            "| A | B |\n|---|---|\n| <script>x</script> | ok |\n")
        assert "<script" not in out
        assert "&lt;script&gt;" in out

    def test_html_inside_a_heading_is_escaped(self):
        assert "<script" not in render_markdown("## <script>x</script>")

    def test_only_http_links_become_anchors(self):
        assert '<a href="https://example.com"' in render_markdown("[x](https://example.com)")
        # A javascript: target is not a link pattern this renderer accepts.
        out = render_markdown("[x](javascript:alert(1))")
        assert "<a " not in out
        assert "javascript:alert(1)" in out    # rendered as inert text

    def test_links_carry_noopener(self):
        out = render_markdown("[x](https://example.com)")
        assert 'rel="noopener noreferrer"' in out
        assert 'target="_blank"' in out


class TestReportShape:
    """A realistic report renders end to end without losing its content."""

    REPORT = """# GMG EGX Intelligence — Weekly Report

> **SYNTHETIC DEMONSTRATION DATA**
> Every figure is fictional.

*Period: 2026-08-24 to 2026-08-31*

## 1. Executive Summary

The EGX30 benchmark declined 0.30% over the week.

## 3. Biggest Winners

| Ticker | Move | Attributed to |
|---|---:|---|
| **AMOC** | +10.18% | Board meeting outcome |

## 9. Macro

**This system has no macroeconomic data feed.**

- Central Bank policy rate
- EGP/USD spot

---

*Not investment advice.*
"""

    def test_every_section_survives(self):
        out = render_markdown(self.REPORT)
        for fragment in ("Executive Summary", "Biggest Winners", "Macro",
                         "EGX30 benchmark declined", "AMOC",
                         "no macroeconomic data feed", "Central Bank policy rate",
                         "Not investment advice"):
            assert fragment in out, fragment

    def test_the_synthetic_warning_is_a_callout(self):
        out = render_markdown(self.REPORT)
        assert 'class="callout warn"' in out
        assert "SYNTHETIC DEMONSTRATION DATA" in out

    def test_no_raw_markdown_leaks_through(self):
        out = render_markdown(self.REPORT)
        for artefact in ("## ", "**Ticker", "|---|", "\n- Central"):
            assert artefact not in out, artefact
