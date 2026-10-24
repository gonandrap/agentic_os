#!/usr/bin/env python3
"""Collapse docstrings and comment blocks to one line each; safe to re-run."""
from __future__ import annotations

import argparse
import ast
import inspect
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = ("src", "tests", "scripts", "evals")
SKIP_PARTS = {".venv", "node_modules", "__pycache__", ".git"}
MAX_SUMMARY = 100

# Docstrings that code or tests read at runtime: left untouched.
# Maps repo-relative path -> qualnames whose docstring is kept ("<module>" for the module's).
KEEP_DOCSTRINGS: dict[str, set[str]] = {
    "src/jarvis/invariants.py": {"check_prefix_stable"},
    "src/jarvis/hooks.py": {"note_prefix"},
    "src/jarvis/catalog.py": {"ObservabilityConfig"},
    "evals/llm/test_one_shot_turn_ab.py": {"<module>"},  # tests/test_one_shot_turn.py greps it
}
# Comment blocks that tests read as source text: kept when the block's first line starts
# with one of these prefixes.
KEEP_COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {}
# Whole regions left alone: (start marker, end marker) lines, per file.
KEEP_COMMENT_REGIONS: dict[str, tuple[str, str]] = {
    "src/jarvis/hooks.py": ("# -- the prompt prefix", "def note_prefix"),
}

PRAGMA = re.compile(
    r"#\s*(noqa|type:|pragma|pyright:|pylint:|mypy:|fmt:|isort:|ruff:|nosec|"
    r"-\*-|vim:|flake8|yapf:|region|endregion|clock-exempt:)", re.I)
CODING = re.compile(r"#.*coding[:=]\s*[-\w.]+")
HEADER = re.compile(r"^#:?\s*([-=─━═_~*#]{2,}|——)")
DIVIDER = re.compile(r"^[\s\-=─━═_~*#]+$")
REF = re.compile(
    r"(docs/[\w./-]+\.md(?:\s*§\s*[\w.\-]+)?)"
    r"|(?<![\w&/])(#\d{2,5})\b|(?:issues|pull)/(\d+)")
ABBREV = {"e.g", "i.e", "vs", "etc", "cf", "approx", "no", "fig", "resp"}


def find_ref(text: str) -> str:
    """The one reference to keep: a spec with a section, else the earliest issue/spec."""
    first = ""
    for m in REF.finditer(text):
        if m.group(1):
            ref = re.sub(r"\s*§\s*", " §", m.group(1))
            if "§" in ref:
                return ref
        elif m.group(2):
            ref = m.group(2)
        else:
            ref = "#" + m.group(3)
        first = first or ref
    return first


def first_sentence(text: str) -> str:
    for m in re.finditer(r"[.!?](?=\s+[A-Z`\"'])", text):
        word = re.search(r"([\w.]+)$", text[: m.start()])
        if word and word.group(1).lower() in ABBREV:
            continue
        return text[: m.start() + 1]
    return text


def summarise(text: str) -> str:
    """One line: first sentence (<= ~100 chars) plus at most one reference."""
    text = text.strip()
    para = re.split(r"\n\s*\n", text, maxsplit=1)[0]
    flat = " ".join(para.split())
    ref = find_ref(text)
    body = first_sentence(flat).strip()
    tail = re.search(r"\s*\(([^()]*)\)$", body)
    if tail and REF.fullmatch(tail.group(1).strip()):  # our own earlier output: re-measure the body alone
        ref, body = tail.group(1).strip(), body[: tail.start()]
    if not body.endswith("..."):
        body = body.rstrip(".").rstrip(":;,—- ").strip()
    if len(body) > MAX_SUMMARY and not (body.endswith("...") and len(body) <= MAX_SUMMARY + 3):
        cut = None
        for sep in (" — ", "; ", " - ", ", "):
            i = body.find(sep, 30, MAX_SUMMARY)
            if i != -1:
                cut = i
                break
        if cut is not None:
            body = body[:cut]
        else:
            body = body[:MAX_SUMMARY].rsplit(" ", 1)[0] + "..."
    if ref and not REF.search(body):
        body = f"{body} ({ref})" if body else ref
    return body.strip()


# ---------------------------------------------------------------- docstrings

def _docstring_nodes(tree: ast.Module):
    def walk(node, scope):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                yield ".".join(scope) or "<module>", first
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from walk(child, scope + [child.name])
            else:
                yield from walk(child, scope)

    yield from walk(tree, [])


def _literal(text: str) -> str:
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if text.endswith('"'):
        text = text[:-1] + '\\"'
    return f'"""{text}"""'


def trim_docstrings(src: str, rel: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    keep = set(KEEP_DOCSTRINGS.get(rel, set()))
    if any(isinstance(n, ast.Name) and n.id == "__doc__" for n in ast.walk(tree)):
        keep.add("<module>")  # the module docstring is read at runtime (argparse help, usage)
    edits = []
    for qual, node in _docstring_nodes(tree):
        if qual in keep:
            continue
        first, last = node.lineno - 1, node.end_lineno - 1
        head = lines[first].encode()[: node.col_offset].decode()
        tail = lines[last].encode()[node.end_col_offset:].decode()
        if head.strip() or (tail.strip() and not tail.strip().startswith("#")):
            continue
        new = summarise(inspect.cleandoc(node.value.value))
        if not new:
            continue
        nl = "\n" if lines[last].endswith("\n") else ""
        trailing = tail.rstrip("\r\n")
        edits.append((first, last, f"{head}{_literal(new)}{trailing}{nl}"))
    for first, last, new in sorted(edits, reverse=True):
        lines[first : last + 1] = [new]
    return "".join(lines)


# ------------------------------------------------------------------ comments

def _keep_verbatim(text: str, row: int) -> bool:
    if row == 1 and text.startswith("#!"):
        return True
    if row <= 2 and CODING.match(text):
        return True
    return bool(PRAGMA.match(text))


def _strip_hash(text: str) -> tuple[str, str]:
    prefix = "#:" if text.startswith("#:") else "#"
    body = text[len(prefix):]
    return prefix, body[1:] if body.startswith(" ") else body


def _comment_line(prefix: str, body: str) -> str:
    return f"{prefix} {body}" if body else prefix


def _collapse_block(texts: list[str], rel: str) -> str:
    prefix, _ = _strip_hash(texts[0])
    bodies = [_strip_hash(t)[1].rstrip() for t in texts]
    if any(texts[0].startswith(p) for p in KEEP_COMMENT_PREFIXES.get(rel, ())):
        return "\n".join(texts)
    if HEADER.match(texts[0]) and not DIVIDER.match(bodies[0]):
        return _comment_line(prefix, summarise(bodies[0]) or bodies[0].strip())
    useful = [b for b in bodies if not DIVIDER.match(b)] or bodies[:1]
    if len(useful) == 1 and DIVIDER.match(useful[0]):
        return _comment_line(prefix, useful[0].strip())
    return _comment_line(prefix, summarise("\n".join(useful)))


def trim_comments(src: str, rel: str) -> str:
    lines = src.splitlines(keepends=True)
    region = KEEP_COMMENT_REGIONS.get(rel)
    lo = hi = 0
    if region:
        for i, ln in enumerate(lines, 1):
            if not lo and ln.startswith(region[0]):
                lo = i
            elif lo and ln.startswith(region[1]):
                hi = i
                break
    toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT and not (lo and lo <= t.start[0] <= hi)]
    edits = []
    block: list = []

    def flush():
        if not block:
            return
        texts = [t.string.rstrip() for t in block]
        row = block[0].start[0]
        indent = lines[row - 1][: block[0].start[1]]
        new = _collapse_block(texts, rel)
        edits.append((row - 1, block[-1].start[0] - 1,
                      "".join(indent + part + "\n" for part in new.split("\n"))))
        block.clear()

    for tok in toks:
        row, col = tok.start
        text = tok.string.rstrip()
        line = lines[row - 1]
        if line[:col].strip():  # trailing comment
            flush()
            if _keep_verbatim(text, row):
                continue
            prefix, body = _strip_hash(text)
            new = _comment_line(prefix, summarise(body))
            if new != text:
                edits.append((row - 1, row - 1,
                              line[:col] + new + ("\n" if line.endswith("\n") else "")))
            continue
        if _keep_verbatim(text, row):
            flush()
            continue
        if block and (row != block[-1].start[0] + 1 or col != block[0].start[1]
                      or text.startswith("#:") != block[0].string.startswith("#:")):
            flush()
        block.append(tok)
    flush()
    for first, last, new in sorted(edits, reverse=True):
        old = "".join(lines[first : last + 1])
        if old.endswith("\n") and not new.endswith("\n"):
            new += "\n"
        lines[first : last + 1] = [new]
    return "".join(lines)


# ------------------------------------------------------------------- driver

def _strip_docstrings(tree: ast.AST) -> str:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body:
            f = body[0]
            if isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant) and isinstance(f.value.value, str):
                node.body = body[1:]
    return ast.dump(tree)


def trim_source(src: str, rel: str) -> str:
    out = trim_comments(trim_docstrings(src, rel), rel)
    if _strip_docstrings(ast.parse(src)) != _strip_docstrings(ast.parse(out)):
        raise ValueError("AST changed outside docstrings")
    return out


def iter_files(root: Path):
    me = Path(__file__).resolve()
    for d in DIRS:
        for p in sorted((root / d).rglob("*.py")):
            if SKIP_PARTS & set(p.parts) or p.resolve() == me:
                continue
            head = p.read_text(encoding="utf-8", errors="replace")[:400].lower()
            if "@generated" in head or "do not edit" in head or "code generated" in head:
                continue
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report files that would change; write nothing")
    ap.add_argument("paths", nargs="*", help="limit to these files (default: all)")
    args = ap.parse_args()
    files = [Path(p).resolve() for p in args.paths] or list(iter_files(ROOT))
    before = {d: 0 for d in DIRS}
    after = dict(before)
    changed = failed = 0
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        top = rel.split("/")[0]
        src = p.read_text(encoding="utf-8")
        try:
            out = trim_source(src, rel)
        except Exception as e:  # refuse to write a file that fails verification
            failed += 1
            print(f"REFUSED {rel}: {e}", file=sys.stderr)
            out = src
        before[top] = before.get(top, 0) + src.count("\n")
        after[top] = after.get(top, 0) + out.count("\n")
        if out != src:
            changed += 1
            if args.check:
                # The marker `hooks.overlong_comment_decision` reads back, and the name CI never printed (issue 1231)
                print(f"OVERLONG {rel}")
            else:
                p.write_text(out, encoding="utf-8")
    for d in DIRS:
        print(f"{d:8} {before[d]:7} -> {after[d]:7} lines")
    print(f"{changed} file(s) {'would change' if args.check else 'rewritten'}, {failed} refused")
    return 1 if failed or (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
