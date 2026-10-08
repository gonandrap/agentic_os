"""What counts as SOURCE NAVIGATION — one classifier, one shell masker.

§3 of
docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md
(`.jarvis/features/fo-b9a3fb06/sections/wo-f4b04708.md`), Neo q1238.

A STDLIB-ONLY LEAF. It imports NOTHING from `jarvis`: `hooks.py` imports it on every
Bash `PreToolUse`, so an import of `catalog` here is a per-command cost. The fleet
reader that reads transcripts against these predicates is `nav_volume.py`.

`_mask_shell_text` and `_statements` live here and `hooks` re-exports them BY IDENTITY.
There is exactly ONE masker in the tree: a copied body passes equality and then drifts
(kn-7f5f2d0d).

The DOC classifiers — `DOC_SUFFIXES`, `DOC_DUMP_COMMANDS`, `dumps_doc`, `_dump_span`,
`is_spec_path` — are §3.1 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md. They are
SIBLINGS of `navigates_source`, whose body is not edited: it is read by `nav_volume` to
COUNT and by `hooks.py_nav_decision` to REFUSE, and the fleet's published baseline was
measured with its current meaning (§2.2, kn-358ffb53).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Bash commands that count as reading or searching code. EXACTLY §5.3's set and no
#: more: `nav_volume.BEFORE_NOTE`'s restated baseline was measured with these six, and a
#: wider set makes the AFTER figure incomparable rather than better.
NAV_COMMANDS = ("cat", "head", "sed", "grep", "rg", "find")

#: Which files make a read a CODE read. `.py` is what `nav_volume.BEFORE_NOTE`'s
#: restated baseline measured.
SOURCE_SUFFIXES = (".py",)

#: Symbol tools, BARE — `is_symbol_call` strips the `mcp__<server>__` prefix, because
#: both `mcp__serena__` and `mcp__plugin_serena_serena__` exist in this fleet.
#: `search_for_pattern` is DELIBERATELY ABSENT: it is text search with a Serena name,
#: and counting it as a symbol call is the vacuity trap kn-a397fb52 documents.
SYMBOL_TOOLS = ("find_symbol", "find_referencing_symbols", "get_symbols_overview",
                "find_declaration", "find_implementations")

#: The TOOLS that are text search. `Bash` is not here and must not be — a worker runs
#: all sorts of legitimate shell; the COMMAND is classified instead.
TEXT_SEARCH_TOOLS = ("Grep", "Glob")

#: `mcp__<server>__` — stripped before a tool name is matched.
MCP_PREFIX = "mcp__"

#: A Pyright/LSP symbol tool, whatever the rest of its name.
LSP_PREFIX = "LSP"

#: The three commands that can sweep the tree without naming a file.
_SWEEP_COMMANDS = ("grep", "rg", "find")

#: Wrappers that run another command, so the word after them is still in command
#: position. `_statements` already drops `time` and every env assignment.
_WRAPPERS = ("sudo", "command", "env", "nohup", "setsid", "time")

#: `sed` reads a file only with `-n`; without it, it is an edit.
_SED_READ_FLAGS = ("--quiet", "--silent")

_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)
_SHELL_COMMENT = re.compile(r"(?:(?<=^)|(?<=\s))#[^\n]*")

#: Shell words that open a compound statement, so the command after them is still in
#: command position: `; do sleep 30; done`.
_KEYWORDS = frozenset({"do", "then", "else", "elif", "{", "(", "!", "time"})


def _mask_shell_text(command: str) -> str:
    """The command with quoted spans and comments blanked, positions preserved.

    An `&` inside a string or a comment is prose. This is the whole difficulty of the
    shell half of §4 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md.
    """
    masked = _QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), command)
    return _SHELL_COMMENT.sub(lambda m: " " * len(m.group(0)), masked)


def _statements(masked: str) -> list[list[str]]:
    """The masked command as word lists, one per statement, keywords stripped.

    Command position is what every arm below tests, and a `;`, `|`, `&` or newline is
    where the next one starts — the same reading `_BACKGROUNDING_WORD` does with a regex.
    """
    out: list[list[str]] = []
    for part in re.split(r"[;&|()\n]", masked):
        words = part.split()
        while words and (words[0] in _KEYWORDS or "=" in words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _command_word(words: list[str]) -> str:
    """What this statement actually runs, past a wrapper and past a path."""
    for index, word in enumerate(words):
        if word in _WRAPPERS:
            continue
        return Path(word).name if index or "/" in word else word
    return ""


def _recursive_flag(args: list[str]) -> bool:
    return any(arg == "--recursive"
               or (arg.startswith("-") and not arg.startswith("--")
                   and ("r" in arg or "R" in arg))
               for arg in args)


def _sweeps_the_tree(word: str, words: list[str]) -> bool:
    """A `grep`/`rg`/`find` over the tree rather than over one named file.

    §3: a sweep navigates source even though no token ends in a configured suffix.
    NOT an argument parser — a flag's value is read as a positional, which is what keeps
    `find . -name Makefile` a sweep and `grep -rn x notes.md` a single-file read.
    """
    if word not in _SWEEP_COMMANDS:
        return False
    args = words[1:]
    positional = [arg for arg in args if not arg.startswith("-")]
    if word != "find" and positional:
        positional = positional[1:]     # `grep`/`rg` read a PATTERN first
    named_files = [arg for arg in positional if Path(arg).suffix]
    if named_files:
        return False
    return bool(positional) or _recursive_flag(args)


def _reads_with_sed(args: list[str]) -> bool:
    return any(arg in _SED_READ_FLAGS
               or (arg.startswith("-") and not arg.startswith("--") and "n" in arg)
               for arg in args)


def navigates_source(command: str, suffixes: tuple[str, ...],
                     commands: tuple[str, ...] = NAV_COMMANDS) -> bool:
    """Whether this Bash command reads or searches SOURCE.

    `suffixes` and `commands` are arguments and never globals, so the fleet reader can
    pass its catalog-configured sets and re-measuring needs no release (§2.3). A chain
    or a pipeline navigates when ANY statement does.

    Masked FIRST: a `.py` inside a quoted string is prose, so
    `git commit -m "fix pricing.py"` is False.
    """
    if not command:
        return False
    for words in _statements(_mask_shell_text(command)):
        word = _command_word(words)
        if word not in commands:
            continue
        args = words[1:]
        if word == "sed" and not _reads_with_sed(args):
            continue
        if suffixes and any(arg.endswith(suffixes) for arg in args):
            return True
        if _sweeps_the_tree(word, words):
            return True
    return False


#: Markdown. A sibling of SOURCE_SUFFIXES, which is unchanged.
DOC_SUFFIXES: tuple[str, ...] = (".md",)

#: The commands that DUMP a file. `grep`, `rg` and `find` are deliberately ABSENT:
#: text search in a .md is legitimate and refusing it is this feature's MUST NOT.
#: `tail` is absent too — measured at 137 tokens a call, it is already cheap.
DOC_DUMP_COMMANDS: tuple[str, ...] = ("cat", "head", "sed")

#: A `sed` address range, read off the RAW command: `_mask_shell_text` blanks the quotes
#: `-n '40,80p'` lives inside, so a masked read finds no range at all.
_SED_SPAN = re.compile(r"(\d+)\s*(?:,\s*(\d+))?\s*p\b")

#: Path components that make a markdown file a SPEC (§2.3 classes 1 and 2).
_DOCS_COMPONENT = "docs"
_FEATURE_COMPONENTS = (".jarvis", "features")

#: §2.3 class 3: a feature child's OWN assigned section. NEVER a spec, at any size —
#: refusing it strands the child on the one file dispatch tells it to read first.
_SECTIONS_COMPONENT = "sections"


def _head_count(args: list[str]) -> int | None:
    """A `head`-style line count (`-40`, `-n 40`), off the statement WORDS: it is
    unquoted, unlike sed's. Read for every non-`sed` dump command, so a catalog-added
    one that takes the same flag is ranged rather than unranged."""
    for index, arg in enumerate(args):
        if arg == "-n" and index + 1 < len(args) and args[index + 1].isdigit():
            return int(args[index + 1])
        digits = arg[2:] if arg.startswith("-n") else arg[1:]
        if arg.startswith("-") and digits.isdigit() and digits:
            return int(digits)
    return None


def _dump_span(command: str,
               commands: tuple[str, ...] = DOC_DUMP_COMMANDS) -> int | None:
    """Lines a dump NAMES: 41 for sed -n '40,80p', 40 for head -40, None for
    cat or a bare head. THE ONE EXTRACTOR, and module-private.

    `None` means UNRANGED — a whole-file dump, always over any bar — and must never be
    spelled as zero (§3.1).

    The statements come from `_statements(_mask_shell_text(command))`, so there is no
    second splitter; sed's addresses are then read off the RAW command, because the
    masker blanked the quotes they live inside.
    """
    # `commands` is an ARGUMENT, the same set `dumps_doc` was given: a catalog-added
    # command the extractor cannot see names no range, so every ranged read of it would
    # be refused at any bar.
    dumps = [words for words in _statements(_mask_shell_text(command))
             if _command_word(words) in commands]
    if not dumps:
        return None
    spans: list[int] = []
    if any(_command_word(words) == "sed" for words in dumps):
        for first, last in _SED_SPAN.findall(command):
            low, high = int(first), int(last or first)
            spans.append(abs(high - low) + 1)
    for words in dumps:
        if _command_word(words) != "sed":
            count = _head_count(words[1:])
            if count is not None:
                spans.append(count)
    # Fewer named ranges than dump statements means one dump named none, so the WHOLE
    # chain is unranged. Deliberately conservative: `cat a.md; sed -n '1,5p' b.md`
    # is None (§3.1).
    if len(spans) < len(dumps):
        return None
    return max(spans)


def dumps_doc(command: str,
              suffixes: tuple[str, ...] = DOC_SUFFIXES,
              commands: tuple[str, ...] = DOC_DUMP_COMMANDS,
              *,
              limit_lines: int | None = None) -> bool:
    """Whether this Bash command DUMPS a markdown file.

    TWO READERS WITH OPPOSITE ERROR COSTS. `nav_volume` passes no `limit_lines` and so
    counts EVERY dump, ranged or not: the baseline must keep the small reads the hook
    will go on allowing, or the AFTER figure shows a drop the refusal never caused.
    `hooks.doc_nav_decision` passes its resolved `worker.doc_read_limit_lines` and gets
    only the dumps worth refusing: unranged, or naming a range WIDER than the bar.
    §2.2's rule — the narrowing is a keyword argument at the hook's call site, never an
    edit to the counted body.

    THIS FUNCTION OWNS THE EXTRACTION: the decision function never parses a `sed`
    expression itself, or the counter and the refusal would disagree about what
    "40 lines" means (§3.1).

    Masked FIRST: a `.md` inside a quoted string is prose, and `sed` without `-n` is an
    edit. It does NOT call `_sweeps_the_tree` — with `grep`/`rg`/`find` absent from the
    command set there is no sweep to classify.
    """
    if not command:
        return False
    found = False
    for words in _statements(_mask_shell_text(command)):
        word = _command_word(words)
        if word not in commands:
            continue
        args = words[1:]
        if word == "sed" and not _reads_with_sed(args):
            continue
        if suffixes and any(arg.endswith(suffixes) for arg in args):
            found = True
            break
    if not found:
        return False
    if limit_lines is None:
        return True
    span = _dump_span(command, commands)
    return span is None or span > limit_lines


def is_spec_path(path: str) -> bool:
    """§2.3's three-valued test. The ONE spelling.

    COMPONENTS, never substrings: `hooks.spec_shape_decision`'s `"specs" not in
    path.parts` is the precedent, and a substring test calls `mydocs/x.md` a spec.
    Class 3 is answered FIRST and returns False.
    """
    if not path:
        return False
    parts = Path(path).parts
    if not parts or not parts[-1].endswith(DOC_SUFFIXES):
        return False
    feature = any(parts[i:i + 2] == _FEATURE_COMPONENTS
                  for i in range(len(parts) - 1))
    if feature and _SECTIONS_COMPONENT in parts:
        return False
    return _DOCS_COMPONENT in parts or feature


def is_symbol_call(tool_name: str, symbol_tools: tuple[str, ...] = SYMBOL_TOOLS) -> bool:
    """Is this tool call a SYMBOL lookup?

    Strips a leading `mcp__<server>__`, so `mcp__serena__find_symbol` and
    `mcp__plugin_serena_serena__find_symbol` both count. `search_for_pattern` is not in
    the set and `Grep`/`Glob` are text search, not symbols.
    """
    if not tool_name:
        return False
    bare = tool_name.rsplit("__", 1)[-1] if tool_name.startswith(MCP_PREFIX) \
        else tool_name
    return bare in symbol_tools or bare.startswith(LSP_PREFIX)
