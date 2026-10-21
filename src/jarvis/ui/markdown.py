"""A markdown subset rendered to safe HTML, escape-first and stdlib only (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""

from __future__ import annotations

import html
import re
import unicodedata

from .. import sections

#: The fence line
_FENCE_RE = re.compile(r"^```(\S*)\s*$")

#: One level of list, and one only (§2.3)
_LIST_RE = re.compile(r"^\s*(?:([-*])|(\d+)[.)])\s+(.*)$")

#: Inline, applied in the order §2.4 fixes: code first
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"\*([^*\n]+)\*|_([^_\n]+)_")

#: Autolinks, with a scheme allow-list of exactly two
_LINK_RE = re.compile(r"https?://(?:&amp;|[^\s<>\"'&])+")

#: Punctuation a sentence ends with, never part of the URL it followed
_LINK_TAIL = ".,;:!?)"


def slug(heading_text: str) -> str:
    """The anchor id for one heading"""
    s = heading_text.rstrip("#").strip()
    # Stdlib fold, so `§` and accents degrade instead of vanishing into one long dash
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "section"


def anchors(doc: str) -> list[tuple[str, str]]:
    """`(heading text, slug)` for every heading, in document order"""
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for m in sections.HEADING_RE.finditer(doc or ""):
        text = m.group(2)
        base = slug(text)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append((text, base if n == 0 else f"{base}-{n}"))
    return out


def _slugs_by_line(doc: str) -> dict[int, str]:
    """Line number (0-based) to slug, for every `HEADING_RE` match in the document"""
    found = anchors(doc)
    return {doc.count("\n", 0, m.start()): found[i][1]
            for i, m in enumerate(sections.HEADING_RE.finditer(doc))
            if i < len(found)}


def anchor_for(doc: str, which: str) -> str | None:
    """The anchor for the section `which` names, or None when it does not resolve"""
    if not (doc and which):
        return None
    i = sections.find_heading(doc, which)
    if i is None:
        return None
    found = anchors(doc)
    return found[i][1] if i < len(found) else None


def render(doc: str) -> str:
    """A safe HTML fragment for one markdown document"""
    escaped = html.escape(doc or "")
    # Slugs come from the RAW headings (the ids readers and links agree on)
    ids = _slugs_by_line(doc or "")
    lines = escaped.split("\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline("\n".join(para)) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal items
        if items:
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items)
                       + f"</{tag}>")
            items = []

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line.strip())
        if fence:
            flush_para()
            flush_list()
            body: list[str] = []
            i += 1
            # An unclosed fence runs to the end of the document rather than being dropped (§2.1)
            while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                body.append(lines[i])
                i += 1
            i += 1
            # Verbatim, already escaped, with NO inline pass: a `*` in a shell command is a glob
            out.append("<pre><code>" + "\n".join(body) + "</code></pre>")
            continue

        head = sections.HEADING_RE.match(line)
        if head:
            flush_para()
            flush_list()
            level = min(6, max(2, len(head.group(1))))  # the page's own <h1> is chrome
            anchor = ids.get(i) or slug(head.group(2))
            out.append(f'<h{level} id="{anchor}">{_inline(head.group(2))}</h{level}>')
            i += 1
            continue

        item = _LIST_RE.match(line)
        if item:
            flush_para()
            if not items:
                ordered = item.group(2) is not None
            items.append(item.group(3))
            i += 1
            continue

        if not line.strip():
            flush_para()
            flush_list()
        else:
            flush_list()
            para.append(line)
        i += 1

    flush_para()
    flush_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Inline markup, code spans first so nothing inside them is re-read"""
    out: list[str] = []
    pos = 0
    for m in _CODE_RE.finditer(text):
        out.append(_emphasis(text[pos:m.start()]))
        out.append(f"<code>{m.group(1)}</code>")
        pos = m.end()
    out.append(_emphasis(text[pos:]))
    return "".join(out)


def _emphasis(text: str) -> str:
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _EM_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    return _LINK_RE.sub(_autolink, text)


def _autolink(m: re.Match[str]) -> str:
    url = m.group(0).rstrip(_LINK_TAIL)
    tail = m.group(0)[len(url):]
    return f'<a href="{url}">{url}</a>{tail}'
