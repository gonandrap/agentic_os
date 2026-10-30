"""What counts as SOURCE NAVIGATION — one classifier, one shell masker (docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md)"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

#: Bash commands that count as reading or searching code
NAV_COMMANDS = ("cat", "head", "sed", "grep", "rg", "find")

#: Which files make a read a CODE read
SOURCE_SUFFIXES = (".py",)

#: Symbol tools, BARE — `is_symbol_call` strips the `mcp__<server>__` prefix
SYMBOL_TOOLS = ("find_symbol", "find_referencing_symbols", "get_symbols_overview",
                "find_declaration", "find_implementations")

#: The TOOLS that are text search
TEXT_SEARCH_TOOLS = ("Grep", "Glob")

#: `mcp__<server>__` — stripped before a tool name is matched
MCP_PREFIX = "mcp__"

#: A Pyright/LSP symbol tool, whatever the rest of its name
LSP_PREFIX = "LSP"

#: The three commands that can sweep the tree without naming a file
_SWEEP_COMMANDS = ("grep", "rg", "find")

#: Wrappers that run another command, so the word after them is still in command position
_WRAPPERS = ("sudo", "command", "env", "nohup", "setsid", "time")

#: Flags by which a sweep names its OWN scope. §6 of... (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
_SCOPE_FLAGS = ("-name", "-iname", "-path", "-ipath", "--include", "-g", "--glob")

#: `sed` reads a file only with `-n`; without it, it is an edit
_SED_READ_FLAGS = ("--quiet", "--silent")

_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)
_SHELL_COMMENT = re.compile(r"(?:(?<=^)|(?<=\s))#[^\n]*")

#: Shell words that open a compound statement
_KEYWORDS = frozenset({"do", "then", "else", "elif", "{", "(", "!", "time"})


def _mask_shell_text(command: str) -> str:
    """The command with quoted spans and comments blanked, positions preserved (docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md)"""
    masked = _QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), command)
    return _SHELL_COMMENT.sub(lambda m: " " * len(m.group(0)), masked)


def _statements(masked: str) -> list[list[str]]:
    """The masked command as word lists, one per statement, keywords stripped"""
    out: list[list[str]] = []
    for part in re.split(r"[;&|()\n]", masked):
        words = part.split()
        while words and (words[0] in _KEYWORDS or "=" in words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _command_word(words: list[str]) -> str:
    """What this statement actually runs, past a wrapper and past a path"""
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
    """A `grep`/`rg`/`find` over the tree rather than over one named file"""
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


def _scope_names_suffix(command: str, masked: str, suffixes: tuple[str, ...]) -> bool:
    """Whether a sweep's own scope flag names one of `suffixes`"""
    if not suffixes:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        words = masked.split()
    for index, word in enumerate(words):
        flag, _, value = word.partition("=")
        if flag not in _SCOPE_FLAGS:
            continue
        scope = value or (words[index + 1] if index + 1 < len(words) else "")
        if scope.endswith(suffixes):
            return True
    return False


def _reads_with_sed(args: list[str]) -> bool:
    return any(arg in _SED_READ_FLAGS
               or (arg.startswith("-") and not arg.startswith("--") and "n" in arg)
               for arg in args)


def navigates_source(command: str, suffixes: tuple[str, ...],
                     commands: tuple[str, ...] = NAV_COMMANDS, *,
                     scoped_sweeps: bool = False) -> bool:
    """Whether this Bash command reads or searches SOURCE (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)"""
    if not command:
        return False
    masked = _mask_shell_text(command)
    for words in _statements(masked):
        word = _command_word(words)
        if word not in commands:
            continue
        args = words[1:]
        if word == "sed" and not _reads_with_sed(args):
            continue
        if suffixes and any(arg.endswith(suffixes) for arg in args):
            return True
        if scoped_sweeps and word in _SWEEP_COMMANDS:
            if _scope_names_suffix(command, masked, suffixes):
                return True
            continue
        if _sweeps_the_tree(word, words):
            return True
    return False


#: A subcommand word; ids, paths and project names (`wo-5a89`, `jarvis_os`) never match
_SUBCOMMAND = re.compile(r"[a-z][a-z-]*\Z")


def jarvis_verb(command: str) -> str | None:
    """The `jarvis` subcommand a Bash command runs (`wo finish`), "" for a bare `jarvis`, None for none"""
    for words in _statements(_mask_shell_text(command or "")):
        if _command_word(words) != "jarvis":
            continue
        start = next(i for i, w in enumerate(words) if w not in _WRAPPERS) + 1
        verb: list[str] = []
        for word in words[start:start + 2]:
            if not _SUBCOMMAND.match(word):
                break
            verb.append(word)
        return " ".join(verb)
    return None


#: Markdown
DOC_SUFFIXES: tuple[str, ...] = (".md",)

#: The path components that say documentation
DOC_DIRS: frozenset[str] = frozenset({"docs", "specs"})

#: The commands that DUMP a file
DOC_DUMP_COMMANDS: tuple[str, ...] = ("cat", "head", "sed")

#: A `sed` address range, read off the RAW command: `_mask_shell_text` blanks the quotes `-n '40,80p'`...
_SED_SPAN = re.compile(r"(\d+)\s*(?:,\s*(\d+))?\s*p\b")

#: Path components that make a markdown file a SPEC (§2.3 classes 1 and 2)
_DOCS_COMPONENT = "docs"
_FEATURE_COMPONENTS = (".jarvis", "features")

#: §2.3 class 3: a feature child's OWN assigned section
_SECTIONS_COMPONENT = "sections"


def _head_count(args: list[str]) -> int | None:
    """A `head`-style line count (`-40`, `-n 40`), off the statement WORDS: it is unquoted, unlike sed's"""
    for index, arg in enumerate(args):
        if arg == "-n" and index + 1 < len(args) and args[index + 1].isdigit():
            return int(args[index + 1])
        digits = arg[2:] if arg.startswith("-n") else arg[1:]
        if arg.startswith("-") and digits.isdigit() and digits:
            return int(digits)
    return None


def _dump_span(command: str,
               commands: tuple[str, ...] = DOC_DUMP_COMMANDS) -> int | None:
    """Lines a dump NAMES: 41 for sed -n '40,80p', 40 for head -40, None for cat or a bare head"""
    # `commands` is an ARGUMENT, the same set `dumps_doc` was given: a catalog-added command the...
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
    # Fewer named ranges than dump statements means one dump named none, so the WHOLE chain is unranged
    if len(spans) < len(dumps):
        return None
    return max(spans)


def dumps_doc(command: str,
              suffixes: tuple[str, ...] = DOC_SUFFIXES,
              commands: tuple[str, ...] = DOC_DUMP_COMMANDS,
              *,
              limit_lines: int | None = None) -> bool:
    """Whether this Bash command DUMPS a markdown file"""
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
    """§2.3's three-valued test (docs/x.md)"""
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
    """Is this tool call a SYMBOL lookup?"""
    if not tool_name:
        return False
    bare = tool_name.rsplit("__", 1)[-1] if tool_name.startswith(MCP_PREFIX) \
        else tool_name
    return bare in symbol_tools or bare.startswith(LSP_PREFIX)
