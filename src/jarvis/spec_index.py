"""Progressive disclosure for markdown: a table of contents, and one section.

An agent that needs §4 of a spec reads the whole file, because reading the whole file is
the only move `cat` offers. The cost is a window spent on twelve sections to use one, and
the habit scales with every spec the OS writes. So the document gets the same two moves
code already has: list what is in it, then open the one part — and the size of each part
is on the list, so the choice is made before anything is read.

§4.1 of docs/specs/2026-10-06-navigate-specs-like-code.md. Stdlib only, and `sections` is
the only `jarvis` import: matching headings is `sections.find_heading`'s job and nowhere
else's, so nothing here re-implements it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import sections

#: The FULL dotted label — `3`, `3.`, `3)`, `3.1`, `4.2.1` — never its first component: a
#: `### 2.1` row numbered `2` sends the caller to `## 2.`, the §4.1 bug one row down.
#: `sections.find_heading` resolves a bare `3` as a digit and `2.1` through its name
#: branch, so both round-trip.
LEADING_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)*)(?=[.):\s]|$)")


@dataclass(frozen=True)
class Section:
    """One heading of a document, and the size of the section it opens."""

    number: str
    name: str
    level: int
    line: int
    tokens: int


def toc(markdown: str) -> list[Section]:
    """Every heading, in reading order, with an ESTIMATE of its section's size.

    `tokens` is characters // 4 — an estimate, because a tokenizer is a dependency and
    this feature adds none. It counts the heading's own slice: its line down to the next
    heading of the same or a higher level, exactly what `section` returns for it.

    `number` is the heading's full dotted label when it has one — `2.1`, not `2` — and
    `""` when it has none, so `number or name` is always a ref that resolves back to
    THIS heading. Headings come from `sections.HEADING_RE` and no other matcher.
    """
    heads = [(m.start(), len(m.group(1)), m.group(2))
             for m in sections.HEADING_RE.finditer(markdown)]
    out: list[Section] = []
    for i, (start, level, text) in enumerate(heads):
        end = len(markdown)
        for pos, lvl, _text in heads[i + 1:]:
            if lvl <= level:
                end = pos
                break
        numbered = LEADING_NUMBER_RE.match(text)
        out.append(Section(
            number=numbered.group(1) if numbered else "",
            name=text,
            level=level,
            line=markdown.count("\n", 0, start) + 1,
            tokens=len(markdown[start:end].rstrip()) // 4,
        ))
    return out


def section(markdown: str, which: str) -> str | None:
    """One section by number or name, or None — `sections.extract_section` verbatim."""
    return sections.extract_section(markdown, which)
