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

#: A markdown path named in free text
DOC_RE = re.compile(r"[\w./-]*[\w-]\.md\b")


def find_refs(text: str) -> list[tuple[str, str]]:
    """Every (path, section) reference in the text, deduplicated in reading order"""
    out: list[tuple[str, str]] = []
    for m in REF_RE.finditer(text):
        ref = (m.group(3).strip(), (m.group(1) or m.group(2)).strip())
        if ref not in out:
            out.append(ref)
    return out


def unreferenced_docs(text: str) -> list[str]:
    """Markdown paths in the text that NO `find_refs` reference covers, in reading order (#102)"""
    covered = {path for path, _which in find_refs(text)}
    out: list[str] = []
    for m in DOC_RE.finditer(text):
        path = m.group(0)
        if path not in covered and path not in out:
            out.append(path)
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


def heading_outline(markdown: str) -> list[str]:
    """Every heading line, in order, level included — and NO body text"""
    return [f"{m.group(1)} {m.group(2)}" for m in HEADING_RE.finditer(markdown)]


#: One labelled snapshot of N resolved sections, and the label's own shape
SECTION_LABEL_RE = re.compile(r"^(\S+\.md) § (.+):$", re.MULTILINE)


def label_sections(blocks: list[tuple[str, str, str]]) -> str:
    return "\n\n".join(f"{p} § {which}:\n{text}" for p, which, text in blocks)


def labelled_sections(text: str) -> list[tuple[str, str]]:
    """The `(path, which)` of every label `label_sections` wrote"""
    return [(m.group(1), m.group(2)) for m in SECTION_LABEL_RE.finditer(text)]


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
