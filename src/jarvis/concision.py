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


#: `jarvis wo finish` reached through a `cd … &&` chain, a shell wrapper, or an absolute
#: path -- the same breadth `gh_pr_create_args` needed once workers turned out not to
#: have `gh` on PATH.
_FINISH = re.compile(r"(?:^|/)jarvis$")


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
        if not _FINISH.search(word) or words[i + 1:i + 3] != ["wo", "finish"]:
            continue
        for j, arg in enumerate(words[i + 3:], start=i + 3):
            if arg in ("&&", "||", ";", "|"):
                break
            flag, sep, inline = arg.partition("=")
            if flag != "--summary":
                continue
            if sep:
                return inline
            if j + 1 < len(words):
                return words[j + 1]
        return None
    return None


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
