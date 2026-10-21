"""The project's OKF knowledge map, and the one playbook a worker's brief carries.

A project that ships an Open Knowledge Format bundle at `docs/os/` (index -> playbooks
-> concepts) gets ONE playbook in its worker prompt, chosen by the order's kind and
title, and fetches every deeper level on demand. docs/os/architecture/worker-context.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUNDLE_DIR = Path("docs") / "os"
PLAYBOOK_DIR = "playbooks"
_LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]+)\)")


@dataclass(frozen=True)
class Playbook:
    key: str
    title: str
    description: str
    body: str
    rank: int = 99
    kinds: tuple[str, ...] = ()
    match: tuple[str, ...] = ()
    child: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def _scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value.strip("'\"")


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Frontmatter (the flat `key: value` subset OKF concepts here use) and body."""
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text
    meta: dict[str, Any] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip() and not key.startswith((" ", "#")):
            meta[key.strip()] = _scalar(value)
    return meta, body


def bundle_root(project_path: Path) -> Path | None:
    root = Path(project_path) / BUNDLE_DIR
    return root if (root / "index.md").is_file() else None


def playbooks(root: Path) -> list[Playbook]:
    out = []
    for path in sorted((root / PLAYBOOK_DIR).glob("*.md")):
        if path.name in ("index.md", "log.md"):
            continue
        meta, body = parse(path.read_text())
        if not meta.get("type"):
            continue
        out.append(Playbook(
            key=path.stem, title=str(meta.get("title") or path.stem),
            description=str(meta.get("description") or ""), body=body.strip(),
            rank=int(meta.get("jarvis_rank", 99)),
            kinds=tuple(meta.get("jarvis_kinds") or ()),
            match=tuple(str(m).lower() for m in meta.get("jarvis_match") or ()),
            child=bool(meta.get("jarvis_child")), meta=meta))
    return sorted(out, key=lambda p: (p.rank, p.key))


def _matches(term: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None


def classify(wo: dict[str, Any], books: list[Playbook]) -> Playbook | None:
    """The first playbook, by rank, whose kind, child flag or keywords fit the order."""
    kind = str(wo.get("kind") or "worker")
    first_line = (wo.get("description") or "").strip().split("\n", 1)[0]
    text = f"{wo.get('title') or ''} {first_line}".lower()
    fallback = None
    for book in books:
        if kind in book.kinds or (book.child and wo.get("parent_id")):
            return book
        if kind == "worker" and any(_matches(t, text) for t in book.match):
            return book
        if not (book.kinds or book.match or book.child):
            fallback = fallback or book
    return fallback


def _localise(body: str) -> str:
    """Bundle-relative links become repo paths a worker can open as they stand."""
    return _LINK.sub(lambda m: f"{m.group(1)} (`{BUNDLE_DIR.as_posix()}{m.group(2)}`)",
                     body)


def brief_block(wo: dict[str, Any], project_path: Path) -> list[str]:
    """The map section of a worker prompt: one playbook, everything else by path."""
    root = bundle_root(project_path)
    if root is None:
        return []
    book = classify(wo, playbooks(root))
    if book is None:
        return []
    body = [ln for ln in _localise(book.body).splitlines() if not ln.startswith("# ")]
    index = (BUNDLE_DIR / "index.md").as_posix()
    return [
        f"# Your map: {book.title} (read deeper levels only when you need them)",
        book.description,
        *[ln for ln in body if ln.strip()],
        f"Every concept file is a few lines naming what to read next; `{index}` lists "
        f"them all.",
    ]
