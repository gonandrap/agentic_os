"""In-text references to design-artifact sections, and their extraction (docs/superpowers/specs/exporter.md)"""

from __future__ import annotations

import re

#: A question that should have been a paragraph plus a reference
QUESTION_WARN_CHARS = 1500
QUESTION_MAX_CHARS = 4000

#: `section 3 of ...
REF_RE = re.compile(
    r"section\s+(?:\"([^\"]+)\"|(\d+))\s+of\s+[^\"\n]{0,40}?\"([^\"\n]+)\"",
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def find_refs(text: str) -> list[tuple[str, str]]:
    """Every (path, section) reference in the text, deduplicated in reading order"""
    out: list[tuple[str, str]] = []
    for m in REF_RE.finditer(text):
        ref = (m.group(3).strip(), (m.group(1) or m.group(2)).strip())
        if ref not in out:
            out.append(ref)
    return out


def find_heading(markdown: str, which: str) -> int | None:
    """Index into `HEADING_RE`'s matches for `which`, or None (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
    want = which.strip().strip('"').lower()
    for i, m in enumerate(HEADING_RE.finditer(markdown)):
        low = m.group(2).lower()
        if want.isdigit():
            if low == want or re.match(rf"{re.escape(want)}[.):\s]", low):
                return i
        elif want in low:
            return i
    return None


def extract_section(markdown: str, which: str) -> str | None:
    """One section of a markdown document, heading included, or None"""
    heads = [(m.start(), len(m.group(1)), m.group(2)) for m in HEADING_RE.finditer(markdown)]
    target = find_heading(markdown, which)
    if target is None:
        return None
    start, level, _ = heads[target]
    end = len(markdown)
    for pos, lvl, _text in heads[target + 1:]:
        if lvl <= level:
            end = pos
            break
    return markdown[start:end].rstrip()


def clip_at_heading(markdown: str, max_chars: int) -> tuple[str, list[str]]:
    """The prefix ending at the last heading that fits, and the heading LINES dropped (docs/superpowers/specs/2026-09-25-plan-review-reads-the-spec-the-os-holds.md)"""
    if len(markdown) <= max_chars:
        return markdown, []
    heads = [(m.start(), m.group(0).strip()) for m in HEADING_RE.finditer(markdown)]
    kept_end = 0
    for pos, _line in heads:
        if 0 < pos <= max_chars:
            kept_end = pos
    # No heading boundary inside the budget: a hard cut beats returning nothing
    if kept_end == 0:
        kept_end = max_chars
    return (markdown[:kept_end].rstrip(),
            [line for pos, line in heads if pos >= kept_end])
