"""Progressive disclosure for markdown: a table of contents, and one section.

An agent that needs §4 of a spec reads the whole file, because reading the whole file is
the only move `cat` offers. The cost is a window spent on twelve sections to use one, and
the habit scales with every spec the OS writes. So the document gets the same two moves
code already has: list what is in it, then open the one part — and the size of each part
is on the list, so the choice is made before anything is read.

§4.1 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md. Stdlib only, and `sections` is
the only `jarvis` import: matching headings is `sections.find_heading`'s job and nowhere
else's, so nothing here re-implements it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

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


#: Directories never walked. An unpruned walk makes §4.2's "sub-second scan" false on the
#: first project with a virtualenv — `.venv` alone carries more markdown than the repo.
#: `worktrees` is pruned because a worktree is a COPY of the whole tree: unpruned, every
#: document comes back once per live worktree, the `limit` budget is spent on duplicates,
#: and a sibling BRANCH's text is ranked above the caller's own under the same filename.
#: `.jarvis` is KEPT and must stay kept: a feature child's materialised spec section lives
#: there at the REGISTERED ROOT and in no worktree, and it is exactly the document a child
#: is looking for (Neo ruling on q1372).
PRUNED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox",
    ".mypy_cache", ".pytest_cache", "worktrees",
})

#: Characters of context per hit. Wide enough for a sentence, narrow enough that the
#: `limit` of 40 hits still fits a terminal — a paragraph per hit is the dump this verb
#: exists to replace.
CONTEXT_CHARS = 120


@dataclass(frozen=True)
class Hit:
    """One SECTION that matches, and the ref that opens it."""

    path: str
    section: str
    line: int
    context: str


def search(root: Path, words: str, *, suffixes: tuple[str, ...] = (".md",),
           limit: int = 40) -> list[Hit]:
    """Sections of `root`'s documents containing every term of `words` — §4.1.

    SECTION granularity and deduplicated per section, first match winning: the caller's
    next call is `jarvis spec section <path> <ref>`, so a second hit in the same section
    is a repeat of the same next step. A grep line list would make the reader do the
    mapping the toc already knows.

    `limit` is a second bound on top of the prune (Neo ruling, q1372): a query with a
    common term cannot return a dump however small the tree.
    """
    terms = [w.lower() for w in words.split()]
    if not terms:
        return []
    out: list[Hit] = []
    for path in _documents(root, suffixes):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:     # unreadable file: not a match, never a failed scan
            continue
        if not all(t in text.lower() for t in terms):
            continue
        heads = toc(text)
        seen: set[str] = set()
        for number, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            if not all(t in low for t in terms):
                continue
            ref = _section_ref(heads, number)
            if ref in seen:
                continue
            seen.add(ref)
            out.append(Hit(path=path.relative_to(root).as_posix(), section=ref,
                           line=number, context=line.strip()[:CONTEXT_CHARS]))
            if len(out) >= limit:
                return out
    return out


def _section_ref(heads: list[Section], line: int) -> str:
    """The ref of the last heading at or above `line` — `""` before the first heading."""
    found = ""
    for s in heads:
        if s.line > line:
            break
        found = s.number or s.name
    return found


def _documents(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Every candidate file under `root`, in a stable order, pruned by `PRUNED_DIRS`."""
    out: list[Path] = []
    for where, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNED_DIRS)
        for name in sorted(filenames):
            if name.endswith(suffixes):
                out.append(Path(where) / name)
    return out
