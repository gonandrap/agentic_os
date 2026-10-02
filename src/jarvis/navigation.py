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
"""

from __future__ import annotations

import re
from pathlib import Path

#: Bash commands that count as reading or searching code. EXACTLY §5.3's set and no
#: more: the BEFORE figure (41.3%) was measured with these six, and a wider set makes
#: the AFTER figure incomparable rather than better.
NAV_COMMANDS = ("cat", "head", "sed", "grep", "rg", "find")

#: Which files make a read a CODE read. `.py` because that is what the 41.3% measured.
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
