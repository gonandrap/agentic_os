"""The house style every worker writes in, and the cap the OS enforces it with.

Design: docs/superpowers/specs/2026-09-19-concision-enforced.md. The measurement that
made it necessary is SS1 there: the two skills shipped in August reached every worker and
were invoked zero times in 142 sessions, while the median finish summary went 40 -> 344
words. Delivery was never the problem, so this module is about effect.

Nothing here imports `catalog`. The hook that calls it runs on every Bash command in
every worker, and paying ~60ms to parse the catalog for a number that is fixed at spawn
would be a 39% tax on a ~155ms process (the same argument `JARVIS_GATES` is passed by
env for, `dispatch._write_worker_settings`).
"""

from __future__ import annotations

import os
import re
import shlex

#: The cap on a question argument, re-exported and NOT re-chosen: the hook's cap and the
#: 4000 in `sections.py` are THE SAME RULE at two layers — this one before the command
#: runs, `ops.ask_question` after it. A second number here would mean a question the hook
#: waved through and `ops` refused, or the reverse. `sections` imports `re` and nothing
#: else, so it costs this module nothing of what the docstring above protects.
from .sections import QUESTION_MAX_CHARS as QUESTION_MAX_CHARS

#: Words allowed in `jarvis wo finish --summary` before the PreToolUse hook refuses it.
#: Three times the July median of 40 and a fifth of September's p90 of 560 -- generous on
#: purpose (SS5.3): a denial costs a whole extra turn, and every turn re-sends the whole
#: conversation at the cache-write rate, so a cap tight enough to catch good summaries
#: costs more than the verbosity it prevents.
DEFAULT_SUMMARY_MAX_WORDS = 120

#: Set per work order by `dispatch._write_worker_settings` from the catalog's
#: `concision.summary_max_words`. Absent means the default; `0` disables the check.
SUMMARY_CAP_ENV = "JARVIS_SUMMARY_MAX_WORDS"

#: Set per work order by `dispatch._write_worker_settings` from the work order's own
#: `append_system_prompt`, falling back to the catalog's `worker.append_system_prompt` --
#: the same resolution `worker_session.briefing` uses for `--append-system-prompt`, so a
#: subagent is told what its parent was told. Env for the module docstring's reason.
STANDING_PROMPT_ENV = "JARVIS_APPEND_SYSTEM_PROMPT"

#: Markers around the injected block. `evals/llm/test_house_style_ab.py` builds its
#: WITHOUT arm by cutting between these, so the two arms stay byte-equal everywhere else
#: -- kn-fe226ab1's finding that an A/B which re-composes its arms measures nothing.
HOUSE_STYLE_BEGIN = "<!-- house-style:begin -->"
HOUSE_STYLE_END = "<!-- house-style:end -->"


def summary_cap(env: dict[str, str] | None = None) -> int:
    """The word cap in force, or 0 when the check is off.

    A value that will not parse is the default rather than an error: this decides
    whether to refuse a worker's `finish`, and a typo in a catalog should not be able to
    either block every finish or silently disable the rule.
    """
    raw = (env if env is not None else os.environ).get(SUMMARY_CAP_ENV)
    if raw is None or raw == "":
        return DEFAULT_SUMMARY_MAX_WORDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SUMMARY_MAX_WORDS
    return max(value, 0)


def word_count(text: str) -> int:
    """Words as a reader counts them.

    Whitespace-split, which over-counts a summary carrying a code fragment. That is the
    forgiving direction for everything except the thing being measured, and the cap is
    set loosely enough (SS5.3) that the difference cannot decide a real case.
    """
    return len(text.split())


#: The `jarvis` word itself, reached through a `cd … &&` chain, a shell wrapper, or an
#: absolute path -- the same breadth `gh_pr_create_args` needed once workers turned out
#: not to have `gh` on PATH. Used by `finish_summary` and by `_segments` below.
_JARVIS = re.compile(r"(?:^|/)jarvis$")

#: The same word in RAW, unparsed text. Read by `unparseable_jarvis_command` ALONE, which
#: is the only caller entitled to it: kn-21d73ac2's defect is a denylist matching raw text
#: to decide an ALLOW, and this decides a REFUSAL, where matching too widely is safe.
_JARVIS_RAW = re.compile(r"\bjarvis\b")


def unparseable_jarvis_command(command: str) -> bool:
    """Whether this is a `jarvis` command whose quoting no one can parse.

    §5 of docs/superpowers/specs/2026-09-26-bounded-model-inputs.md, round-1 review: every
    parsed check below yields nothing when `shlex.split` raises, so `jarvis wo send wo-x
    "$(git diff)` with an unbalanced quote fell through all of them while the shell went
    ahead and expanded the payload into this worker's context anyway.
    """
    try:
        shlex.split(command)
    except ValueError:
        return bool(_JARVIS_RAW.search(command))
    return False


#: `jarvis wo finish`'s long options exactly as `src/jarvis/cli.py:692-705` spells them.
#: Needed because the CLI's parser is argparse with the default `allow_abbrev=True`, so it
#: accepts any UNAMBIGUOUS PREFIX of each — `--summ`, `--s` — and a check matching
#: `--summary` exactly is kn-21d73ac2's fail-open ("refuse unambiguous ABBREVIATIONS of a
#: denied long option"). Keep in step with the parser: an option added there that shares a
#: prefix with `--summary` makes that prefix ambiguous in both places at once.
_WO_FINISH_OPTIONS = ("--summary", "--pr", "--evidence", "--abandon")


def _option_matches(word: str, name: str, siblings: tuple[str, ...]) -> bool:
    """Whether `word` is how argparse would spell `name` among `siblings`.

    Exact equality, or a prefix no other sibling also starts with — argparse's own
    `allow_abbrev` rule, so the hook accepts exactly the spellings the CLI does.
    """
    if word == name:
        return True
    if not word.startswith("--") or word == "--" or not name.startswith(word):
        return False
    return not any(other != name and other.startswith(word) for other in siblings)


def finish_summary(command: str) -> str | None:
    """The `--summary` of a `jarvis wo finish` in this command, or None.

    None means "not this hook's business": not a finish, no `--summary`, or a command
    shlex cannot parse. Deliberately narrow, for `gh_pr_create_args`' reason -- a hook
    firing on commands it does not really understand costs more than the leak it stops.
    """
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


#: Characters allowed in a `jarvis wo send` / `wo assume` body before the PreToolUse hook
#: refuses it. Measured fleet-wide 2026-09-28 across all five project stores: the 140
#: CLI-authored `jarvis wo send` bodies ran median 807, p95 3153, p99 3707, max 5307
#: chars, and every one over 3000 was inspected and was legitimate prose (a PR review
#: relay, an addendum to a brief), not a pasted artefact; the 929 `jarvis wo assume`
#: bodies ran median 390, p95 769, max 2340. 6000 clears the largest legitimate message
#: observed (5307) with headroom, so the refusal cannot fire on a real brief.
#:
#: What it does NOT catch: a diff short enough to fit under 6000 typed out by hand. That
#: is accepted — `unbounded_substitution` below covers the route a payload actually
#: arrives by, and a cap tight enough to catch a hand-typed diff would refuse real briefs.
DEFAULT_MESSAGE_MAX_CHARS = 6000

#: Set per work order by `dispatch._write_worker_settings` from the catalog's
#: `concision.message_max_chars`. Absent means the default; `0` disables the check.
MESSAGE_CAP_ENV = "JARVIS_MESSAGE_MAX_CHARS"


def message_cap(env: dict[str, str] | None = None) -> int:
    """The character cap on a message argument, or 0 when the check is off.

    `summary_cap`'s posture and its reason: a typo in a catalog must not be able either
    to refuse every message in the fleet or to switch the rule off in silence, so
    anything that does not parse is the DEFAULT rather than 0 and rather than an error.

    A NEGATIVE value is the same case, and is the default too — not clamped to 0, which
    would be the silent switch-off this docstring promises not to do. `catalog` refuses a
    negative on the way in, so a negative reaching here is a hand-set env var, i.e. a
    typo for a cap and never a considered "off": `0` is how off is asked for.
    """
    raw = (env if env is not None else os.environ).get(MESSAGE_CAP_ENV)
    if raw is None or raw == "":
        return DEFAULT_MESSAGE_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MESSAGE_MAX_CHARS
    return value if value >= 0 else DEFAULT_MESSAGE_MAX_CHARS


#: The `jarvis` subcommands whose text argument is capped, as the CLI spells them
#: (`src/jarvis/cli.py`: `wo send`, `wo assume`, `wo ask`), mapped to which cap applies.
#: `neo ask` is not a CLI spelling today — `wo ask` is the worker's route to Neo — and is
#: listed so the shape the spec names is covered if it ever is.
#:
#: Each of these takes the text as its SECOND positional (the first is the id), and none
#: of them has a boolean flag, which is what lets the walk below treat `--flag` as
#: consuming the word after it.
_CAPPED: dict[tuple[str, str], str] = {
    ("wo", "send"): "message",
    ("wo", "assume"): "message",
    ("wo", "ask"): "question",
    ("neo", "ask"): "question",
}

_TEXT_POSITIONAL = 1

_CHAIN = ("&&", "||", ";", "|", "&")


def _segments(command: str) -> list[tuple[str, list[str]]]:
    """Every capped `jarvis` invocation in the command: its subcommand as typed, and the
    argument words up to the next chain operator.

    Parses with `shlex.split` and works on the PARSED WORDS only — kn-21d73ac2: a
    denylist applied to unparsed text fails open, because the shell strips the quotes
    before anything downstream sees the payload. An unparseable command yields nothing,
    the posture `finish_summary`'s docstring argues for: not this hook's business.
    """
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
    """One `(subcommand, kind, text)` per capped argument in this command.

    `kind` is `"message"` (`wo send`, `wo assume`) or `"question"` (`wo ask`), which is
    which cap the caller applies. Empty list means "not this hook's business".
    """
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


#: Producers whose output has no bound the caller chose: a diff, a log, a file, a page,
#: a tail. First token matched after any leading directory, so `/bin/cat` counts.
#: Bounded producers are deliberately absent and must stay absent — `git rev-parse HEAD`,
#: `date`, `pwd` and `git branch --show-current` are exactly what the rule asks a worker
#: to pass INSTEAD of a payload, so refusing them would leave no way to write a reference.
_UNBOUNDED = (
    "git diff", "git log", "gh pr diff", "gh pr view",
    "cat", "curl", "tail", "head",
)

#: `$( … )` and its backtick twin. Non-greedy and nesting-blind: this decides whether to
#: refuse, and the inner command's first tokens are all that decision needs.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`", re.DOTALL)


def unbounded_substitution(word: str) -> str | None:
    """The producer named, when this word substitutes the output of an unbounded one.

    This is the half `ops` cannot do. By the time `ops.ask_question` or `ops.send_message`
    sees the text, the shell has already expanded `$(git diff)` into the worker's own
    argv AND its context, so the flood is paid for whatever `ops` then decides about the
    length. Only a `PreToolUse` check runs before that.
    """
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
    """`(subcommand, producer)` per capped `jarvis` invocation carrying a substitution.

    Scans the argument words JOINED, not one at a time: an UNQUOTED `$(git diff)` is two
    words to `shlex` (`$(git` and `diff)`) and neither carries the substitution alone.
    """
    out: list[tuple[str, str]] = []
    for subcommand, args in _segments(command):
        producer = unbounded_substitution(" ".join(args))
        if producer:
            out.append((subcommand, producer))
    return out


def house_style() -> str:
    """The rules injected into every turn, between the markers the A/B cuts on.

    A digest of `assets/skills/caveman` (level `full`) and `assets/skills/i-have-adhd`,
    not the two files inlined: they are ~2,900 tokens together and this is ~400, which is
    the difference between a rule that can ride every turn and one that cannot. The
    skills stay shipped and keep the examples, the intensity table and the wenyan modes;
    a worker that wants them opens them.

    SS4.2 is the half that must never be trimmed for brevity -- it is what keeps this a
    style rule rather than a correctness risk.
    """
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


def subagent_context(env: dict[str, str] | None = None) -> str:
    """What a `SubagentStart` hook injects: the house style, plus the project's standing
    worker instructions when it has any.

    A Task subagent inherits its parent's CLAUDE.md, skills, settings and hooks, and
    NONE of its `--append-system-prompt`, `--agent` persona or `SessionStart`
    `additionalContext` -- measured on Claude Code 2.1.278, spec
    docs/superpowers/specs/2026-09-19-concision-enforced.md SS5.1. So the two things a
    worker was given out of band have to be re-delivered here or the subagent writes
    without either.

    The standing prompt is read from the environment, never from `catalog`: see the
    module docstring.
    """
    standing = (env if env is not None else os.environ).get(STANDING_PROMPT_ENV) or ""
    parts = [house_style()]
    if standing.strip():
        parts.append("# Standing instructions for this project\n\n" + standing.strip())
    return "\n\n".join(parts)
