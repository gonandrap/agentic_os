"""The house style every worker writes in, and the cap the OS enforces it with (docs/superpowers/specs/2026-09-19-concision-enforced.md)"""

from __future__ import annotations

import os
import re
import shlex

#: Words allowed in `jarvis wo finish --summary` before the PreToolUse hook refuses it
DEFAULT_SUMMARY_MAX_WORDS = 120

#: Set per work order by `dispatch._write_worker_settings` from the catalog's...
SUMMARY_CAP_ENV = "JARVIS_SUMMARY_MAX_WORDS"

#: Set per work order by `dispatch._write_worker_settings` from the work order's own...
STANDING_PROMPT_ENV = "JARVIS_APPEND_SYSTEM_PROMPT"

#: Markers around the injected block
HOUSE_STYLE_BEGIN = "<!-- house-style:begin -->"
HOUSE_STYLE_END = "<!-- house-style:end -->"


def summary_cap(env: dict[str, str] | None = None) -> int:
    """The word cap in force, or 0 when the check is off"""
    raw = (env if env is not None else os.environ).get(SUMMARY_CAP_ENV)
    if raw is None or raw == "":
        return DEFAULT_SUMMARY_MAX_WORDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SUMMARY_MAX_WORDS
    return max(value, 0)


def word_count(text: str) -> int:
    """Words as a reader counts them"""
    return len(text.split())


#: The `jarvis` word itself, reached through a `cd … &&` chain
_JARVIS = re.compile(r"(?:^|/)jarvis$")

#: The same word in RAW, unparsed text
_JARVIS_RAW = re.compile(r"\bjarvis\b")


def unparseable_jarvis_command(command: str) -> bool:
    """Whether this is a `jarvis` command whose quoting no one can parse (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
    try:
        shlex.split(command)
    except ValueError:
        return bool(_JARVIS_RAW.search(command))
    return False


#: `jarvis wo finish`'s long options exactly as `src/jarvis/cli.py:692-705` spells them
_WO_FINISH_OPTIONS = ("--summary", "--pr", "--evidence", "--abandon")


def _option_matches(word: str, name: str, siblings: tuple[str, ...]) -> bool:
    """Whether `word` is how argparse would spell `name` among `siblings`"""
    if word == name:
        return True
    if not word.startswith("--") or word == "--" or not name.startswith(word):
        return False
    return not any(other != name and other.startswith(word) for other in siblings)


def finish_summary(command: str) -> str | None:
    """The `--summary` of a `jarvis wo finish` in this command, or None"""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words):
        if not _JARVIS.search(word) or words[i + 1:i + 3] != ["wo", "finish"]:
            continue
        for j, arg in enumerate(words[i + 3:], start=i + 3):
            if arg in ("&&", "||", ";", "|"):
                break
            flag, sep, inline = arg.partition("=")
            if not _option_matches(flag, "--summary", _WO_FINISH_OPTIONS):
                continue
            if sep:
                return inline
            if j + 1 < len(words):
                return words[j + 1]
        return None
    return None


#: Characters allowed in a `jarvis wo send` body before the PreToolUse hook refuses it
DEFAULT_MESSAGE_MAX_CHARS = 6000

#: Characters allowed in a `jarvis wo assume` body — ONE decision fits in them (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §1)
DEFAULT_ASSUME_MAX_CHARS = 1000

#: Characters allowed in a question argument; `sections.QUESTION_MAX_CHARS` re-exports it (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §1)
DEFAULT_QUESTION_MAX_CHARS = 2000

#: The name `ops`, `sections` and the tests read the question cap by
QUESTION_MAX_CHARS = DEFAULT_QUESTION_MAX_CHARS

#: Set per work order by `dispatch._write_worker_settings` from the catalog's...
MESSAGE_CAP_ENV = "JARVIS_MESSAGE_MAX_CHARS"
ASSUME_CAP_ENV = "JARVIS_ASSUME_MAX_CHARS"
QUESTION_CAP_ENV = "JARVIS_QUESTION_MAX_CHARS"


def _char_cap(env: dict[str, str] | None, name: str, default: int) -> int:
    """One character cap off the environment: anything unusable gives the default"""
    raw = (env if env is not None else os.environ).get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def message_cap(env: dict[str, str] | None = None) -> int:
    """The character cap on a message argument, or 0 when the check is off"""
    return _char_cap(env, MESSAGE_CAP_ENV, DEFAULT_MESSAGE_MAX_CHARS)


def assume_cap(env: dict[str, str] | None = None) -> int:
    """The character cap on a `wo assume` body, or 0 when the check is off"""
    return _char_cap(env, ASSUME_CAP_ENV, DEFAULT_ASSUME_MAX_CHARS)


def question_cap(env: dict[str, str] | None = None) -> int:
    """The character cap on a question argument, or 0 when the check is off"""
    return _char_cap(env, QUESTION_CAP_ENV, DEFAULT_QUESTION_MAX_CHARS)


#: The `jarvis` subcommands whose text argument is capped
_CAPPED: dict[tuple[str, str], str] = {
    ("wo", "send"): "message",
    ("wo", "assume"): "assume",
    ("wo", "ask"): "question",
    ("neo", "ask"): "question",
}

_TEXT_POSITIONAL = 1

_CHAIN = ("&&", "||", ";", "|", "&")


def _segments(command: str) -> list[tuple[str, list[str]]]:
    """Every capped `jarvis` invocation in the command: its subcommand as typed"""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    out: list[tuple[str, list[str]]] = []
    for i, word in enumerate(words):
        if not _JARVIS.search(word):
            continue
        pair = tuple(words[i + 1:i + 3])
        if len(pair) != 2 or pair not in _CAPPED:
            continue
        args: list[str] = []
        for arg in words[i + 3:]:
            if arg in _CHAIN:
                break
            args.append(arg)
        out.append((f"{pair[0]} {pair[1]}", args))
    return out


def jarvis_payload_args(command: str) -> list[tuple[str, str, str]]:
    """One `(subcommand, kind, text)` per capped argument in this command"""
    out: list[tuple[str, str, str]] = []
    for subcommand, args in _segments(command):
        kind = _CAPPED[tuple(subcommand.split())]  # type: ignore[index]
        positionals: list[str] = []
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg.startswith("-"):
                skip = "=" not in arg
                continue
            positionals.append(arg)
        if len(positionals) > _TEXT_POSITIONAL:
            out.append((subcommand, kind, positionals[_TEXT_POSITIONAL]))
    return out


#: Producers whose output has no bound the caller chose: a diff, a log, a file, a page, a tail
_UNBOUNDED = (
    "git diff", "git log", "gh pr diff", "gh pr view",
    "cat", "curl", "tail", "head",
)

#: `$( … )` and its backtick twin
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`", re.DOTALL)


def unbounded_substitution(word: str) -> str | None:
    """The producer named, when this word substitutes the output of an unbounded one"""
    for match in _SUBSTITUTION.finditer(word):
        inner = " ".join((match.group(1) or match.group(2) or "").split())
        if not inner:
            continue
        head, _, rest = inner.partition(" ")
        inner = f"{head.rsplit('/', 1)[-1]} {rest}".strip()
        for producer in _UNBOUNDED:
            if inner == producer or inner.startswith(producer + " "):
                return producer
    return None


def unbounded_producers(command: str) -> list[tuple[str, str]]:
    """`(subcommand, producer)` per capped `jarvis` invocation carrying a substitution"""
    out: list[tuple[str, str]] = []
    for subcommand, args in _segments(command):
        producer = unbounded_substitution(" ".join(args))
        if producer:
            out.append((subcommand, producer))
    return out


def house_style() -> str:
    """The rules injected into every turn, between the markers the A/B cuts on"""
    return "\n".join([
        HOUSE_STYLE_BEGIN,
        "# House style: how you write, everywhere",
        "",
        "This governs EVERY byte you generate this turn -- work-order summaries and "
        "messages, Neo questions, your final answer, commit messages, PR bodies, code "
        "comments, issue text. Not a suggestion and not a mode you enter; it is the "
        "default and it does not lapse. Full rules: your `caveman` and `i-have-adhd` "
        "skills.",
        "",
        "## Compress (caveman, level `full`)",
        "- Drop articles (a/an/the) and filler (just/really/basically/actually/simply).",
        "- Drop pleasantries and hedging. Fragments are fine. Short synonyms: `fix` not "
        "`implement a solution for`, `big` not `extensive`.",
        "- No narration of your own tool calls. No decorative tables or emoji. No "
        "dumping raw logs -- quote the shortest decisive line.",
        "- Never invent abbreviations (cfg/impl/req/res/fn) and never use arrows (->). "
        "Both measure as zero tokens saved and cost the reader. Standard acronyms "
        "(DB/API/HTTP) are fine.",
        "- Never add a word to sound terse. If the plain phrasing is shorter, it wins.",
        "",
        "## Shape (i-have-adhd)",
        "- First line is the answer or the next action. Never context, never a plan.",
        "- Say each thing ONCE across the whole record. The description, your questions, "
        "your messages and your summary are read together -- refer back, do not restate.",
        "- No preamble (`Let me`, `I'll`, `Great question`), no recap of what you just "
        "did, no closing offer of further help.",
        "- Numbered steps for multi-step work. Cap lists at 5; past that, split into "
        "`now` and `later`.",
        "- Be specific about size, cost and risk. `15 minutes if tests cover this` is "
        "usable; `some work` is not.",
        "- Errors matter-of-fact: cause and fix, no `Uh oh`.",
        "",
        "## Never compressed",
        "Correctness beats brevity every time:",
        "- Exact error strings, numbers, units, code blocks, API and symbol names, CLI "
        "commands -- verbatim, always.",
        "- The words not/never/no/only/except. Dropping one flips the meaning.",
        "- Security warnings, irreversible-action confirmations, and any multi-step "
        "sequence where fragment order could be misread: write those in full prose.",
        "- Failing test output, and the things you did NOT do.",
        "",
        f"Your `jarvis wo finish --summary` is capped at {DEFAULT_SUMMARY_MAX_WORDS} "
        "words and the OS refuses a longer one. The summary is the headline; the detail "
        "goes in the final message of your turn, which is captured onto the record "
        "verbatim.",
        HOUSE_STYLE_END,
    ])


#: Three lines, not a section: injected on every `SubagentStart` fleet-wide (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)
SPEC_NAVIGATION = "\n".join([
    "# Specs and docs are navigated, never read whole",
    "A spec is a tree of headings: `jarvis spec toc <path>` lists its sections, "
    "`jarvis spec section <path> <n|name>` returns one, and "
    "`jarvis spec search \"<words>\"` finds which spec covers a thing. Reach for those "
    "before `Read` with no `limit`, `cat` or `sed -n` on a `.md` file.",
    "`grep`, `rg` and `find` over markdown stay fine for text questions — a phrase, a "
    "filename. Reading the whole document to reach one section is the waste.",
])


def subagent_context(env: dict[str, str] | None = None) -> str:
    """What a `SubagentStart` hook injects: the house style (docs/superpowers/specs/2026-09-19-concision-enforced.md)"""
    standing = (env if env is not None else os.environ).get(STANDING_PROMPT_ENV) or ""
    parts = [house_style(), SPEC_NAVIGATION]
    if standing.strip():
        parts.append("# Standing instructions for this project\n\n" + standing.strip())
    return "\n\n".join(parts)
