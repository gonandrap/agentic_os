"""In-text references to design-artifact sections, and their extraction.

The ruling behind this module (wo-e4a359cb): a question to Neo is ONE PARAGRAPH that
argues from a design artifact and references it explicitly, by section — never by
pasting the document. The reference lives inside the sentence, no flag and no schema:

    ... from section 3 of design doc "docs/superpowers/specs/exporter.md": should ...
    ... per section "Data model" of the design doc "docs/superpowers/specs/exporter.md" ...

`find_refs` reads those shapes out of free text. `extract_section` cuts ONE section out
of a markdown document — its heading line down to the next heading of the same or a
higher level — by number or by name. `ops.ask_question` resolves the pair and hands Neo
the section alongside the paragraph, which is the whole point: the answerer sees exactly
the design context the question argues from, and none of the rest.
"""

from __future__ import annotations

import re

#: A question that should have been a paragraph plus a reference. Above the warning the
#: asker is nudged; above the cap the ask is refused outright with the fix named. The
#: numbers are the wo-e4a359cb ruling: production worker questions ran 3–7.5KB of pasted
#: context while Neo already held the work order's title and description.
QUESTION_WARN_CHARS = 1500
QUESTION_MAX_CHARS = 4000

#: `section 3 of ... "path"` / `section "Name" of the ... "path"`. The words between
#: `of` and the quoted path are free ("the design doc", "the spec", nothing) but bounded,
#: so a quoted path later in the sentence is not swallowed into the wrong reference.
REF_RE = re.compile(
    r"section\s+(?:\"([^\"]+)\"|(\d+))\s+of\s+[^\"\n]{0,40}?\"([^\"\n]+)\"",
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

#: A markdown path named in free text. Markdown only: a design artifact is a `.md`
#: document, and a source file named in the same sentence is not a spec citation.
DOC_RE = re.compile(r"[\w./-]*[\w-]\.md\b")


def find_refs(text: str) -> list[tuple[str, str]]:
    """Every (path, section) reference in the text, deduplicated in reading order."""
    out: list[tuple[str, str]] = []
    for m in REF_RE.finditer(text):
        ref = (m.group(3).strip(), (m.group(1) or m.group(2)).strip())
        if ref not in out:
            out.append(ref)
    return out


def unreferenced_docs(text: str) -> list[str]:
    """Markdown paths in the text that NO `find_refs` reference covers, in reading order.

    The other half of the in-text form. A sentence that names a design document without
    naming a section of it is the shape the user decision of 2026-10-14 (Neo learning
    #102) refuses: Neo is handed cited SECTIONS — N under one labelled snapshot, bounded by
    one total (`ops.SECTION_SNAPSHOT_CHARS`) — so "the spec says so" with a whole document
    beside it has nothing to resolve. `ops` turns each of these into a refusal that shows
    the asker the form above.
    """
    covered = {path for path, _which in find_refs(text)}
    out: list[str] = []
    for m in DOC_RE.finditer(text):
        path = m.group(0)
        if path not in covered and path not in out:
            out.append(path)
    return out


def find_heading(markdown: str, which: str) -> int | None:
    """Index into `HEADING_RE`'s matches for `which`, or None. THE ONE MATCHER.

    `which` is a number — matched against numbered headings like `## 3. Failure
    handling` — or a name, matched case-insensitively as a substring of the heading
    text. A number that matches no numbered heading returns None rather than guessing
    at "the Nth heading": a wrong section delivered confidently is worse than a
    reference the asker is told did not resolve.

    Split out of `extract_section` so the anchor a page links to and the section a
    child's brief was cut from cannot disagree — §3 of
    docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md. A second matcher
    is a child whose brief gets section 4 and whose link lands on section 5.
    """
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
    """Every heading line, in order, level included — and NO body text. THE ONE MATCHER.

    Neo ruling Q1880: a plan review reads the spec's STRUCTURE, never the document.
    """
    return [f"{m.group(1)} {m.group(2)}" for m in HEADING_RE.finditer(markdown)]


#: One labelled snapshot of N resolved sections, and the label's own shape. Neo ruling
#: Q1883: a payload may carry several sections, so each body names the section it is.
SECTION_LABEL_RE = re.compile(r"^(\S+\.md) § (.+):$", re.MULTILINE)


def label_sections(blocks: list[tuple[str, str, str]]) -> str:
    return "\n\n".join(f"{p} § {which}:\n{text}" for p, which, text in blocks)


def labelled_sections(text: str) -> list[tuple[str, str]]:
    """The `(path, which)` of every label `label_sections` wrote."""
    return [(m.group(1), m.group(2)) for m in SECTION_LABEL_RE.finditer(text)]


def extract_section(markdown: str, which: str) -> str | None:
    """One section of a markdown document, heading included, or None.

    Matching is `find_heading`'s and nowhere else's; this adds only the slicing.
    """
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
