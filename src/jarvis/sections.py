"""In-text references to design-artifact sections, and their extraction (docs/superpowers/specs/exporter.md)"""

from __future__ import annotations

import re

from . import concision

#: A question that should have been a paragraph plus a reference
QUESTION_WARN_CHARS = 1500
#: `concision` owns the number; this is the name `ops` and the tests read it by (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §1)
QUESTION_MAX_CHARS = concision.DEFAULT_QUESTION_MAX_CHARS

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


#: `6.2 item 5`, `§6.2`, `section 7 step 2`: a numbered heading, optionally one list item under it
CITE_RE = re.compile(
    r"(?:(?:§\s*|\bsection\s+)(\d+(?:\.\d+)*)|\b(\d+(?:\.\d+)+))"
    r"(?:\s*,?\s*(?:item|step|point)\s+(\d+))?", re.IGNORECASE)
_ITEM_RE = re.compile(r"^([ \t]*)(\d+)[.)]\s", re.MULTILINE)
_LIST_LINE_RE = re.compile(r"^([ \t]*)(?:\d+[.)]|[-*+])\s")


def _numbered_heading(markdown: str, num: str) -> int | None:
    want = re.compile(rf"§?\s*{re.escape(num)}(?:[.):\s]|$)")
    for i, m in enumerate(HEADING_RE.finditer(markdown)):
        if want.match(m.group(2)):
            return i
    return None


def _section_at(markdown: str, index: int) -> str:
    heads = list(HEADING_RE.finditer(markdown))
    start, level = heads[index].start(), len(heads[index].group(1))
    end = next((h.start() for h in heads[index + 1:] if len(h.group(1)) <= level),
               len(markdown))
    return markdown[start:end].rstrip()


def list_item(markdown: str, n: str) -> str | None:
    """Numbered item `n` of the outermost numbered list in `markdown`, continuation lines included"""
    found = [m for m in _ITEM_RE.finditer(markdown) if m.group(2) == n]
    if not found:
        return None
    m = min(found, key=lambda x: len(x.group(1)))
    indent = len(m.group(1))
    lines = markdown[m.start():].splitlines()
    out, blank = [lines[0]], False
    for line in lines[1:]:
        if not line.strip():
            blank = True
            out.append(line)
            continue
        own = len(line) - len(line.lstrip())
        if HEADING_RE.match(line) or (own <= indent and (blank or _LIST_LINE_RE.match(line))):
            break
        blank = False
        out.append(line)
    return "\n".join(out).rstrip()


def cited_excerpts(markdown: str, text: str) -> list[str]:
    """The smallest pieces of `markdown` that `text` cites by number"""
    out: list[str] = []
    for m in CITE_RE.finditer(text):
        num, item = m.group(1) or m.group(2), m.group(3)
        index = _numbered_heading(markdown, num)
        if index is None or (index == 0 and not item):
            continue  # not here, or the whole section the caller already has
        piece = _section_at(markdown, index)
        if item:
            piece = list_item(piece, item) or piece
        if not any(piece in kept for kept in out):
            out = [kept for kept in out if kept not in piece] + [piece]
    return out


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
