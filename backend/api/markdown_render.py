"""A small, safe Markdown renderer for GMG's generated reports.

Reports are produced by this system, but they quote headlines, disclosure titles
and provider names that came from outside it. So the renderer **escapes first
and formats second**: no path through this module can emit markup that came from
the source text.

It covers exactly the subset the report generator produces — headings, tables,
bullet and numbered lists, blockquotes, bold, italic, inline code, horizontal
rules and paragraphs — rather than pulling in a full CommonMark implementation
for eleven constructs. Anything it does not recognise is rendered as an escaped
paragraph, which is the safe failure.
"""
from __future__ import annotations

import html
import re

__all__ = ["render_markdown"]

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)([^\*]+?)(?<!\s)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_DIVIDER = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")


def _inline(text: str) -> str:
    """Escape, then apply inline formatting to the escaped text.

    Order matters: escaping after formatting would destroy the tags we just
    created, and formatting after escaping is what makes injection impossible.
    """
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    # Links are rebuilt from the escaped text, and only for http(s) URLs, so a
    # javascript: or data: target cannot be produced.
    out = _LINK.sub(
        lambda m: (
            f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">'
            f"{m.group(1)}</a>"
        ),
        out,
    )
    return out


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _numeric(cell: str) -> bool:
    """Right-align a column that holds numbers, the way a table should."""
    bare = re.sub(r"[*_`]", "", cell).strip()
    return bool(re.fullmatch(r"[+\-]?[\d,]+(\.\d+)?%?x?", bare))


def render_markdown(text: str | None) -> str:
    """Render a report to HTML. Returns an empty string for empty input."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # --- Horizontal rule -------------------------------------------------
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            out.append("<hr>")
            index += 1
            continue

        # --- Heading ---------------------------------------------------------
        heading = _HEADING.match(stripped)
        if heading:
            level = min(len(heading.group(1)) + 1, 6)   # h1 in the doc becomes h2
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # --- Table -----------------------------------------------------------
        if stripped.startswith("|") and index + 1 < total and _TABLE_DIVIDER.match(lines[index + 1].strip()):
            header = _split_row(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < total and lines[index].strip().startswith("|"):
                rows.append(_split_row(lines[index].strip()))
                index += 1
            # Align a column right when the body values in it are numeric.
            aligned = [
                all(_numeric(row[col]) for row in rows if col < len(row) and row[col])
                and any(col < len(row) and row[col] for row in rows)
                for col in range(len(header))
            ]
            out.append('<div class="tbl-scroll"><table class="tbl compact"><thead><tr>')
            for col, cell in enumerate(header):
                css = ' class="num"' if col < len(aligned) and aligned[col] else ""
                out.append(f"<th{css}>{_inline(cell)}</th>")
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for col, cell in enumerate(row):
                    css = ' class="num"' if col < len(aligned) and aligned[col] else ""
                    out.append(f"<td{css}>{_inline(cell)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
            continue

        # --- Blockquote ------------------------------------------------------
        if stripped.startswith(">"):
            quote: list[str] = []
            while index < total and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            body = " ".join(part for part in quote if part)
            out.append(f'<blockquote class="callout warn">{_inline(body)}</blockquote>')
            continue

        # --- Bullet list -----------------------------------------------------
        if _BULLET.match(stripped):
            out.append("<ul>")
            while index < total and _BULLET.match(lines[index].strip()):
                out.append(f"<li>{_inline(_BULLET.match(lines[index].strip()).group(1))}</li>")
                index += 1
            out.append("</ul>")
            continue

        # --- Numbered list ---------------------------------------------------
        if _NUMBERED.match(stripped):
            out.append("<ol>")
            while index < total and _NUMBERED.match(lines[index].strip()):
                out.append(f"<li>{_inline(_NUMBERED.match(lines[index].strip()).group(2))}</li>")
                index += 1
            out.append("</ol>")
            continue

        # --- Paragraph -------------------------------------------------------
        paragraph: list[str] = []
        while index < total and lines[index].strip() and not (
            lines[index].strip().startswith(("#", ">", "|", "- ", "* "))
            or _NUMBERED.match(lines[index].strip())
            or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", lines[index].strip())
        ):
            paragraph.append(lines[index].strip())
            index += 1
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
        else:
            index += 1

    return "\n".join(out)
