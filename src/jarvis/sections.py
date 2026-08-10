"""In-text references to design-artifact sections, and their extraction.

The ruling behind this module (wo-e4a359cb): a question to Neo is ONE PARAGRAPH that
argues from a design artifact and references it explicitly, by section — never by
pasting the document. The reference lives inside the sentence, no flag and no schema:

    ... from section 3 of design doc "docs/specs/exporter.md": should ...
    ... per section "Data model" of the design doc "docs/specs/exporter.md" ...

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


def find_refs(text: str) -> list[tuple[str, str]]:
    """Every (path, section) reference in the text, deduplicated in reading order."""
    out: list[tuple[str, str]] = []
    for m in REF_RE.finditer(text):
        ref = (m.group(3).strip(), (m.group(1) or m.group(2)).strip())
        if ref not in out:
            out.append(ref)
    return out


def extract_section(markdown: str, which: str) -> str | None:
    """One section of a markdown document, heading included, or None.

    `which` is a number — matched against numbered headings like `## 3. Failure
    handling` — or a name, matched case-insensitively as a substring of the heading
    text. A number that matches no numbered heading returns None rather than guessing
    at "the Nth heading": a wrong section delivered confidently is worse than a
    reference the asker is told did not resolve.
    """
    heads = [(m.start(), len(m.group(1)), m.group(2)) for m in HEADING_RE.finditer(markdown)]
    want = which.strip().strip('"').lower()
    target = None
    for i, (_, _, text) in enumerate(heads):
        low = text.lower()
        if want.isdigit():
            if low == want or re.match(rf"{re.escape(want)}[.):\s]", low):
                target = i
                break
        elif want in low:
            target = i
            break
    if target is None:
        return None
    start, level, _ = heads[target]
    end = len(markdown)
    for pos, lvl, _text in heads[target + 1:]:
        if lvl <= level:
            end = pos
            break
    return markdown[start:end].rstrip()
