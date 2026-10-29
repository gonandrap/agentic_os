"""What a worker is told, split into a minimal core and full briefings on demand"""

from __future__ import annotations

from .concision import DEFAULT_ASSUME_MAX_CHARS, DEFAULT_SUMMARY_MAX_WORDS as SUMMARY_MAX_WORDS

WO_PLACEHOLDER = "<wo-id>"
PROJECT_PLACEHOLDER = "<project>"

#: The one spelling of a spec citation (`sections.REF_RE`); there is no `--section` flag
SECTION_FORM = 'from section 3 of design doc "docs/superpowers/specs/feature.md": …'


def assume_shape(wo_id: str, *, tail: str = "", cite_spec: bool = True) -> str:
    """The `wo assume` bullet every brief carries: ONE decision, the cap, the in-text reference form (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §5)"""
    return (
        f"- `jarvis wo assume {wo_id} \"...\"`: ONE DECISION you made with NO doubt "
        f"— choice, rejected alternative, why; ≤{DEFAULT_ASSUME_MAX_CHARS} "
        f"chars; a second decision (no `Also:` — Neo returns bundles) is a second "
        f"`jarvis wo assume` citing it (`per Q1234`/`per assumption #2`)."
        # Dropped in the CORE only: `CORE_BUDGET_CHARS` leaves no room for it there
        + (f" Cite a spec BY SECTION the same way (e.g. `{SECTION_FORM}`), down to the "
           f"item you rely on (`6.2 item 5`), never by pasting it and never by naming a "
           f"whole document." if cite_spec else "")
        + tail)

#: Budget for the rendered core contract block (between "# Operating contract" and "# Full... (docs/superpowers/specs/2026-08-22-agent-concision.md)
CORE_BUDGET_CHARS = 3900


# -- the git briefing, replacing Claude Code's own

# Model id -> the name Claude Code puts in the Co-Authored-By trailer
MODEL_ATTRIBUTION_NAMES: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-mythos-5": "Claude Mythos 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
}

#: The PR footer, verbatim from the CLI bundle
PR_ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def attribution_name(model: str | None) -> str:
    """The display name for the Co-Authored-By trailer"""
    m = (model or "").strip().lower()
    hit = [k for k in MODEL_ATTRIBUTION_NAMES if m.startswith(k)]
    return MODEL_ATTRIBUTION_NAMES[max(hit, key=len)] if hit else "Claude"


def git_briefing(model: str | None = None) -> str:
    """The git conventions, as a STATIC block Jarvis owns"""
    return "\n".join([
        "# Git",
        "",
        "Claude Code's built-in git instructions and its git-status snapshot are "
        "switched off for you on purpose: both are rebuilt from the working tree on "
        "every turn, and that churn invalidated the prompt cache for your whole "
        "conversation each time you resumed. This block replaces them and is identical "
        "on every turn. Run `git status` / `git log` yourself whenever you need the "
        "state of the tree — it is not in your prompt.",
        "",
        "- Interactive flags (`-i`, e.g. `git rebase -i`, `git add -i`) are not "
        "supported in this environment.",
        "- Use the `gh` CLI for GitHub operations (PRs, issues, API).",
        "- Commit your work and open a PR when the task is done — your work order is "
        "the ask. Never commit to or push the default branch.",
        "- End git commit messages with:",
        f"Co-Authored-By: {attribution_name(model)} <noreply@anthropic.com>",
        "- End PR bodies with:",
        PR_ATTRIBUTION,
    ])


class UnknownSection(KeyError):
    """Asked for a briefing section that does not exist"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"unknown briefing section {name!r} — valid sections: "
            + ", ".join(SECTION_HOOKS))

    def __str__(self) -> str:  # KeyError.__str__ would repr-quote the message
        return self.args[0]


# One line per section, mirrored into the core's index
SECTION_HOOKS: dict[str, str] = {
    "contract": "the complete operating contract, with the reasoning the core omits",
    "gates": "the privileged-action protocol: requests, verdicts, and what a "
             "dismissal means",
    "record": "what the outside world sees of your work, and the finishing protocol "
              "in full",
    "navigation": "Serena first, grep second — how to find code before you go "
                  "exploring",
    "concision": "what a comment, a PR body and a message to the record are each "
                 "allowed to contain",
    "knowledge": "the OS knowledge base: reading it on demand, writing back what "
                 "you learn",
}


def section_names() -> list[str]:
    return list(SECTION_HOOKS)


def render_section(name: str, *, wo_id: str | None = None,
                   project: str | None = None,
                   gates_enabled: tuple[str, ...] | None = None,
                   serena: bool = True,
                   tool_search: str = "cli",
                   transport: str = "background") -> str:
    """One full section, rendered with real ids when the caller has them"""
    wo = wo_id or WO_PLACEHOLDER
    proj = project or PROJECT_PLACEHOLDER
    if name == "contract":
        return contract_section(wo, proj)
    if name == "gates":
        return gates_section(wo, enabled=gates_enabled)
    if name == "record":
        return record_section(wo, transport)
    if name == "navigation":
        return navigation_section(serena, tool_search=tool_search)
    if name == "concision":
        return concision_section()
    if name == "knowledge":
        return knowledge_section(proj)
    raise UnknownSection(name)


# -- the core

#: The two turn transports (`claude_cli.TRANSPORT_*`), spelled here so this module stays a leaf
TRANSPORT_BACKGROUND = "background"
TRANSPORT_HEADLESS = "headless"


def long_work_bullet(transport: str) -> str:
    """How a worker runs work that outlasts one command, which depends on what a turn IS (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    if transport == TRANSPORT_HEADLESS:
        return (
            "- **A turn is one-shot and NOTHING wakes you when a background job "
            "ends.** Long work — a whole-suite run, a CI watcher, a "
            "`jarvis-implementer` or `jarvis-spec-writer` seat — runs in the BACKGROUND "
            "(`run_in_background: true`); the FOREGROUND call is REFUSED. Collect it "
            "with `BashOutput`/`TaskOutput` every 3-4 minutes, never one call past 240 "
            "seconds: your prompt cache lives 5 minutes and a call after it re-sends "
            "this whole conversation. Nothing else may be backgrounded, and never END a "
            "turn with a task uncollected — the Stop hook refuses it — or claim you "
            "will be re-invoked.")
    return (
        "- **Long work runs in the background, and it wakes you.** A whole-suite run, "
        "a CI watcher, a subagent: `run_in_background: true`, then keep working or "
        "end your turn — its result arrives as a notification. NEVER poll it: no "
        "`sleep` loops, no `TaskOutput`/`BashOutput` check-ins, no `git diff` to see "
        "how far a subagent got. Each of those re-reads this whole conversation.")


def crew_block(require_crew: bool) -> list[str]:
    """What an ordinary worker is told about its two subagent seats (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    if require_crew:
        # spec 2026-09-23-the-crew-a-worker-must-use.md SS6
        return [
            "",
            "# Your crew",
            "You are a LEAD with two subagents, and delegating is the job, not a "
            "courtesy: spec writing goes to `jarvis-spec-writer`, code and tests go to "
            "`jarvis-implementer` (TDD, failing test first).",
            "- YOURS, and neither of theirs: git, the PR, every `jarvis` command, the "
            "work-order record (messages, assumptions, the finish summary), and REVIEW "
            "of what they hand back — it is a draft until you have read it.",
            "- Your OWN `Edit`/`Write` inside the worktree is refused by a hook, so "
            "route it through `jarvis-implementer`.",
        ]
    return [
        "",
        "# Subagents",
        "DO THE WORK YOURSELF: read, edit, test, commit. A subagent re-reads the code "
        "you already read and you then re-read what it wrote, so delegating a change "
        "you could make yourself costs it twice. `jarvis-implementer` and "
        "`jarvis-spec-writer` exist for a LARGE, self-contained chunk you can specify "
        "completely, or independent work you want done in parallel.",
        "- A subagent inherits none of your context: brief it fully. Its hand-back is its "
        "report — read the final diff ONCE before you commit; do not watch its progress.",
    ]


def core_contract(wo_id: str, title: str, project: str, has_knowledge: bool,
                  gate_names: tuple[str, ...] = (),
                  kind: str = "worker",
                  transport: str = TRANSPORT_BACKGROUND,
                  require_crew: bool = False) -> list[str]:
    """The compressed operating contract: only the invariants a worker cannot be allowed to discover by..."""
    from .sections import QUESTION_MAX_CHARS

    lines = [
        "# Operating contract",
        "You MUST follow it. This is the compressed core; the full contract with "
        "all its reasoning is one read-only command away — see the index below.",
        # The commit/PR half of this bullet is gone from the CORE: `git_briefing` states both in every...
        "- Work only inside your assigned worktree (you start in it).",
        f"- **The PR title MUST start with `[{wo_id}] `** — e.g. "
        f"`[{wo_id}] {title[:40]}`. `gh pr create` with any other title is blocked.",
        f"- **Neo is your first responder. Any doubt goes to it.** "
        f"`jarvis wo ask {wo_id} \"<your question>\"`, then END YOUR TURN; the "
        f"answer arrives as your next user turn, usually within a minute. "
        f"Normal: it is not an escalation and it does not "
        f"interrupt the user. The trigger is DOUBT, not importance — if you catch "
        f"yourself weighing options or thinking \"either would work\", you are in "
        f"doubt: Ask BEFORE you build on it, not after. A question is one "
        f"paragraph: the decision, the options, your recommendation — no pasted "
        f"context; ≤{QUESTION_MAX_CHARS} chars.",
        "  - Do not talk yourself out of asking: \"It's reversible\", \"it's only "
        "an implementation detail\", \"I'll note it as an assumption\" are "
        "rationalisations for guessing.",
        *([
            "  - But LOOK IT UP FIRST when a headline in the knowledge-base index "
            "below names the area you are unsure about — a lookup is not a doubt.",
        ] if has_knowledge else []),
        assume_shape(wo_id, cite_spec=False,
                     tail=" It should be RARE. Record EVERY such call, including "
                          "the small and obvious ones: the only audit trail."),
        *([
            f"- READ the OS knowledge base before you touch an area it covers — it "
            f"is INDEXED at the end of this prompt, not pasted into it: "
            f"`jarvis learn show <id>` for a body, `jarvis learn search \"<term>\" "
            f"--project {project}` to find which entries match.",
        ] if has_knowledge else []),
        f"- The OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project} --topic \"<topic>\"`. Your "
        f"own notes and memory files are invisible to everyone else.",
        *([
            f"- Privileged actions ({', '.join(gate_names)}) are gated, NOT "
            f"forbidden: an attempt is blocked, filed for review, and the gate's "
            f"full instructions arrive when one fires. You make a stronger case by "
            f"asking first — `jarvis gate request {wo_id} \"<the exact command>\" "
            f"--why \"<why this action should proceed>\" --evidence \"<what a "
            f"reviewer can check>\"`; each kind asks for something different and "
            f"`jarvis brief gates` says what. If a "
            f"gate fires on a command that ships NOTHING, that is a classifier bug and "
            f"the request above is the wrong move: contest it instead — `jarvis gate "
            f"contest <request-number> --why \"<why this performs no privileged "
            f"action>\"`, by the NUMBER the block printed, never by re-typing the "
            f"command (full protocol: `jarvis brief gates --wo {wo_id}`).",
        ] if gate_names else []),
        long_work_bullet(transport),
        f"- **Point, do not explain; say each thing ONCE.** A code comment is one "
        f"line citing the spec that explains it (`docs/superpowers/specs/`), not "
        f"the explanation. A PR body hints; the diff explains. Never restate what "
        f"is already on this record. Rules: `jarvis brief concision`.",
        f"- When done, ALWAYS run `jarvis wo finish {wo_id} --summary \"...\"` "
        f"(code you wrote needs `--pr <url>` or `--abandon`) and then write your "
        f"complete answer as the last thing you say. The record IS this "
        f"conversation: the last message of every turn is captured verbatim and is "
        f"what the user and Neo decide from. The summary is the HEADLINE — capped "
        f"at {SUMMARY_MAX_WORDS} words, longer REFUSED — your final message is the "
        f"report, and where detail goes.",
        # What to do when review feedback asks for more moved into `record_section` (Neo question 946): this...
        f"- Add `--evidence \"<what you ran and what it showed>\"` to that same "
        f"finish: the tests, evals and checks you ran, and what they reported. It says "
        f"how you know it works, and review feedback may ask for more.",
        f"- **Pass a REFERENCE, never a payload.** No diff, log, file or JSON dump in "
        f"a `jarvis` command, message or question: pass a pull request URL, commit "
        f"SHA, path with a line range, or the command that reproduces it. An "
        f"oversized argument and a `$(git diff …)` are refused before the command "
        f"runs.",
        *(crew_block(require_crew) if kind == "worker" else []),
    ]
    return lines


#: The `navigation` hook for a project that has deselected Serena
NO_SERENA_HOOK = ("Glob and Grep — how to find code here, and what a text search "
                  "cannot answer")


def section_index(wo_id: str, gated: bool, serena: bool = True) -> list[str]:
    """The map of the full sections, mirroring the knowledge-index pattern"""
    lines = [
        "# Full briefings on demand",
        "This prompt is the minimum. The full text behind each part of it is "
        "indexed here, one cheap read-only command away — it only prints text, so "
        "run it freely whenever you enter a section's territory, and BEFORE you "
        "act there:",
        f"`jarvis brief <section> --wo {wo_id}`",
    ]
    for name, hook in SECTION_HOOKS.items():
        if name == "gates" and not gated:
            continue  # never point at territory this project does not have
        if name == "navigation" and not serena:
            hook = NO_SERENA_HOOK
        lines.append(f"- `{name}` — {hook}")
    return lines


# -- the full sections

def _question_shape(wo_id: str) -> list[str]:
    """The full question protocol, shared by the contract section (single source for the sentences the..."""
    from .sections import QUESTION_MAX_CHARS

    return [
        f"- **Neo is your first responder. Any doubt goes to it.** Not just the big "
        f"calls — any point where you are not sure. `jarvis wo ask {wo_id} "
        f"\"<your question>\"`, then END YOUR TURN. The answer arrives as your next "
        f"user turn, usually within a minute, from Neo (the user's delegate) or the "
        f"user. This is the normal, expected way to work: it is not an escalation, "
        f"it does not interrupt the user, and it costs you about a minute. A "
        f"question is one paragraph: the decision, the concrete options, your "
        f"recommendation. Do NOT paste context — whoever answers already holds this "
        f"work order's title and description, and when your paragraph references "
        f"the design artifact it argues from in-text (e.g. `from section 3 of "
        f"design doc \"docs/superpowers/specs/feature.md\": …`) that section is delivered "
        f"alongside it automatically. Questions over {QUESTION_MAX_CHARS} "
        f"characters are refused.",
        "  - The trigger is DOUBT, not importance. If you catch yourself weighing "
        "options, thinking \"either would work\", or picking one because you have "
        "to pick something, you are in doubt: ask. Ask BEFORE you build on it, not "
        "after.",
        "  - Do not talk yourself out of asking. \"It's reversible\", \"it's only "
        "an implementation detail\", \"I'll note it as an assumption\" — those are "
        "rationalisations for guessing. Almost everything is reversible; that is "
        "not the question. The question is whether you would be REBUILDING if you "
        "guessed wrong.",
    ]


def contract_section(wo_id: str = WO_PLACEHOLDER,
                     project: str = PROJECT_PLACEHOLDER) -> str:
    """The complete operating contract"""
    lines = [
        "# Operating contract — full text",
        "You MUST follow this contract (it mirrors the project's OPERATION.md — do "
        "not go looking for that file, everything you need is here):",
        "- Work only inside your assigned worktree (you start in it). Commit your "
        "work and open a PR per this repo's conventions. Never push to main.",
        f"- **The PR title MUST start with `[{wo_id}] `** — e.g. "
        f"`[{wo_id}] <short title>`. It is what ties the pull request back to this "
        f"work order for everyone who never sees Jarvis. `gh pr create` with any "
        f"other title is blocked.",
        *_question_shape(wo_id),
        "  - But LOOK IT UP FIRST if a headline in the knowledge-base index of "
        "your opening prompt names the area you are unsure about: fetch that entry "
        "(`jarvis learn show <id>`) before you ask. A lookup is not a doubt — "
        "re-deciding what a past worker already recorded spends Neo's or the "
        "user's attention for nothing. This applies only when a headline actually "
        "matches; when nothing in the index fits, ask, and never let it become a "
        "reason to go looking instead of recording a call you made with no doubt. "
        "(Your prompt carries the index only when the knowledge base has entries; "
        "with no index there is nothing to look up.)",
        assume_shape(wo_id, tail=" It is the OTHER case and should be RARE: you "
                                 "followed an existing convention, the work order "
                                 "implied it, the codebase left one sensible option. "
                                 "Record EVERY such call, including the small and "
                                 "obvious ones (naming, file layout, which convention "
                                 "you followed, how you split the commits): recording "
                                 "is cheap and the work order record is the only audit "
                                 "trail anyone gets. It is a disclosure of something "
                                 "you were SURE about, never a guess you are hoping "
                                 "nobody checks — if you are guessing, ask instead."),
        f"- Found work that is real but is not THIS work order's job? Do not file "
        f"it yourself and do not leave a note: `jarvis wo defer {wo_id} \"<title>\" "
        f"--why \"<why it should not be done now>\"` (add `--neo-question <id>` if "
        f"you agreed the deferral with Neo, `-d` to brief whoever picks it up). That "
        f"is the whole action — the OS decides where it lands, records which work "
        f"order suggested it, and tells you nothing back, because nothing you do "
        f"next should depend on the answer.",
        f"- READ the OS knowledge base on demand: `jarvis learn show <id>` for an "
        f"entry your prompt's index lists — it is the only verb that returns a "
        f"body — and `jarvis learn search \"<term>\" --project {project}` to find "
        f"which entries match, which answers with headlines, ids and one matching "
        f"line each. Look up any area you are about to "
        f"touch BEFORE you touch it, and before you ask or assume about it — a "
        f"past worker probably already paid for the lesson. A headline or a quoted "
        f"line is never the whole entry: if it looks relevant, `show` it rather "
        f"than acting on the summary.",
        f"- WRITE to it too: the OS knowledge base is the ONLY memory that "
        f"survives you: `jarvis learn add \"...\" --project {project} --topic "
        f"\"<topic>\"`. Anything durable you learn — project state, gotchas, "
        f"conventions, decisions — goes there. Your own memory files, notes and "
        f"scratch docs are invisible to the user, to Neo and to the next worker "
        f"(Jarvis mirrors any memory file you do write, but say it here and it "
        f"lands intact).",
        f"- Alert the human when needed: `jarvis notify --project {project} "
        f"--level warning|critical \"title\" \"body\"`",
        "- Hit a bug in Jarvis OS itself (a `jarvis` command fails, hangs, or does "
        "the wrong thing)? Use your `report-jarvis-bug` skill, then carry on with "
        "this work order. Bugs in THIS project are not Jarvis OS bugs — those go "
        "to the backlog.",
        f"- When done, ALWAYS run: `jarvis wo finish {wo_id} --summary \"...\"` "
        f"and then write your full answer as the last thing you say. If you opened "
        f"a pull request, pass it too: `--pr <url>` — and if you wrote code, that "
        f"is REQUIRED: finishing over commits no pull request covers is refused "
        f"(issue #232), with `--abandon \"<why>\"` as the way through when the work "
        f"is deliberately not being landed. The full finishing protocol and what "
        f"the record demands of every turn: `jarvis brief record --wo {wo_id}`.",
        "",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise. User feedback may arrive as new user turns; treat "
        "it as authoritative for this work order.",
    ]
    return "\n".join(lines)


def turn_section(transport: str) -> list[str]:
    """What a turn IS, in full — `long_work_bullet`'s long form (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    if transport == TRANSPORT_HEADLESS:
        return [
            "# A turn is one-shot",
            "Your session is a single `claude -p --resume` process, and ending a turn "
            "exits it. Everything you started inside it goes too: a backgrounded "
            "command is killed or orphaned, and either way nobody reads its output, "
            "because no event re-invokes a worker when a job it launched finishes. "
            "Exactly three things start your next turn — an answer to `jarvis wo ask`, "
            "a verdict on a gate you requested, a message the user sends you.",
            "",
            "So work that outlasts one command runs in the BACKGROUND and you POLL it: "
            "`run_in_background: true`, then `BashOutput` / `TaskOutput` every 3-4 "
            "minutes until it reports finished, and no single call blocking longer than "
            "240 seconds. The reason is the clock, not taste — your prompt cache lives "
            "5 minutes, so a call more than 300 seconds after the previous one re-sends "
            "this whole conversation at the 1.25x write rate, where a check-in inside "
            "the window is a 0.1x read. This is the ONLY work that may be backgrounded: "
            "a whole-suite test run, a CI watcher, a `jarvis-implementer` or "
            "`jarvis-spec-writer` seat, a long wait. The FOREGROUND call is refused for "
            "those and backgrounding anything else still is. You may also split it "
            "into pieces that each fit inside a turn. If it fits in neither, say so and "
            "ask — a turn that ends on a question is a turn the "
            "OS understands. NEVER end a turn with a task uncollected: the Stop hook "
            "refuses it, and ending kills the task. A turn that ends on \"I'll pick this "
            "up when the "
            "background run finishes\" is the work order abandoned mid-task, and the "
            "user is flagged for it.",
        ]
    return [
        "# Your session stays up between turns",
        "Your session is a Claude Code background session that lives for the whole "
        "work order. A turn ends when you stop talking, but the session does not: "
        "anything you started with `run_in_background: true` keeps running, and when "
        "it finishes its result arrives as a notification that starts your next turn "
        "by itself. A subagent works the same way.",
        "",
        "So work that outlasts one command runs in the BACKGROUND and you do NOT "
        "watch it: no `sleep` loops, no `TaskOutput` / `BashOutput` check-ins, no "
        "`git diff` to see how far a subagent got. Each of those calls re-reads this "
        "whole conversation; waiting for the notification costs nothing. Keep "
        "working on something else, or end the turn. Messages from Neo and the user "
        "wait until the session is idle, so they never cut a background job short.",
    ]


def record_section(wo_id: str = WO_PLACEHOLDER,
                   transport: str = TRANSPORT_BACKGROUND) -> str:
    """What the outside world sees, plus the finishing protocol in full"""
    lines = [
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is "
        "concerned. The last message of every turn you take is captured verbatim "
        "into it, and the user and Neo make their decisions from that record — "
        "neither will ever open this session. So end every turn with the complete "
        "answer: findings, caveats, uncertainties, what you did NOT do, and "
        "absolute paths.",
        "",
        f"`--summary` is the HEADLINE for that answer and is capped at "
        f"{SUMMARY_MAX_WORDS} words — the OS REFUSES a longer one, which costs you a "
        f"whole turn. One or two sentences: what you built and where it landed. It is "
        f"never a substitute for the final message, and never the place to put a "
        f"detail, because the final message is where a detail survives. Evidence goes "
        f"in `--evidence`, not in the summary. Older work orders on this record carry "
        f"500-word summaries; do not copy them.",
        "",
        "# Finishing",
        f"When done, ALWAYS run: `jarvis wo finish {wo_id} --summary \"...\"` and "
        f"then write your full answer as the last thing you say. If you opened a "
        f"pull request, pass it too: `--pr <url>`. That parks the work order in "
        f"'waiting for PR merge', where it stays on the user's open list with the "
        f"link until they merge it, instead of settling as completed work nobody "
        f"is looking at. The daemon closes it itself once the PR merges; a PR "
        f"closed unmerged sends the work order back for review.",
        "",
        "**If you wrote code, that is not optional.** `finish` REFUSES while your "
        "worktree carries commits, or files you never committed, that no pull "
        "request covers — it will tell you what it found, and name any pull-request "
        "URL sitting in your summary that you forgot to pass as `--pr`. Six work "
        "orders once reached `completed` with their code on nothing but a branch, "
        "two of them carrying 3,100 lines that nobody noticed for seven weeks "
        "(GitHub issue #232).",
        "",
        "Deliberately NOT landing what you wrote is a perfectly good answer, and "
        f"the OS only asks that you say it out loud: `--abandon \"<why it is not "
        f"being landed>\"` completes the work order and records the reason on it. "
        f"A spike that proved an approach wrong has produced something worth "
        f"recording and nothing worth merging. What the OS will not accept is "
        f"silence, because silence is indistinguishable from work that was lost.",
        "",
        "# Evidence",
        f"Pass `--evidence \"<what you ran and what it showed>\"` to `jarvis wo "
        f"finish` as well: the tests, evals and manual checks you actually "
        f"performed, and what they reported. It is a different question from "
        f"`--summary`. The summary says what you built and where; the evidence "
        f"says how you know it works, and it is read beside your diff by someone "
        f"who was not in this session and takes nothing on trust. \"Ran the "
        f"suite\" is not evidence; \"`uv run pytest tests/test_thing.py -q` — 412 "
        f"passed, 0 failed, including the 6 new cases\" is. Review feedback may come "
        f"back asking for more — do what it asks, then finish again with the fuller "
        f"account.",
        "",
        "RUN THE TARGETED TESTS, NOT THE WHOLE SUITE. The tests for what you changed "
        "belong in a worker turn; the full suite belongs to CI, which runs it on more "
        "interpreters than you can and whose result the reviewer reads instead of your "
        "claim. Running it yourself is slower, weaker evidence AND expensive: your turn "
        "is one conversation with a five-minute prompt cache, so a single twenty-minute "
        "blocking call re-sends the whole conversation at the cache-WRITE rate on the "
        "next call. One work order paid that seven times over (kn-356c724b).",
        "",
        "DO NOT WAIT FOR CI EITHER — that is the same blocked turn with a different "
        "command in it. Finish as soon as your targeted tests pass and the pull request "
        "is open: the OS holds the validation round until GitHub has reported, and "
        "nothing is billed while it waits.",
        "",
        "Review feedback may come back asking for more: a case you did not cover, "
        "a claim the diff does not support, a check you described but did not "
        "run. That is an ordinary part of finishing. Do the work it asks for, "
        "then run `jarvis wo finish` again with the fuller account — a second "
        "`finish` that only rewords the first one has added nothing.",
        "",
        "Do not stop without finishing: a session that ends with neither "
        f"`jarvis wo finish` nor an open question is invisible work.",
        "",
        *turn_section(transport),
    ]
    return "\n".join(lines)


#: The ToolSearch recovery call, as the worker must type it (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md)
NAV_SELECT_TOOLS = ("find_symbol", "find_referencing_symbols", "get_symbols_overview",
                    "activate_project")
NAV_SELECT_LINE = "select:" + ",".join(
    f"{prefix}{tool}"
    for tool in NAV_SELECT_TOOLS
    for prefix in ("mcp__serena__", "mcp__plugin_serena_serena__"))


#: The markdown half of the posture (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)
_MARKDOWN_NAV = [
    "# Finding things in specs and docs: navigate them like code",
    "A spec is a tree of headings, so read the heading you need and not the file: "
    "`jarvis spec toc <path>` lists the sections, "
    "`jarvis spec section <path> <n|name>` returns one of them, and "
    "`jarvis spec search \"<words>\"` finds which spec says a thing at all.",
    "- `grep`, `rg` and `find` over markdown stay fine for TEXT questions — a phrase, "
    "a filename, who mentions a key. Reading a whole spec to reach one section is the "
    "waste, not text search.",
]


def navigation_core(serena: bool = True, tool_search: str = "cli") -> list[str]:
    """The navigation posture INLINE in the bare worker prompt — spec §3"""
    # Spec 2026-10-06-navigate-specs-like-code.md §6.1
    if not serena:
        return list(_MARKDOWN_NAV)
    tail = [
        "- The bash-first reminder does NOT govern code navigation: `cat`/`sed "
        "-n`/`grep` answer text questions, never symbol ones.",
        "- `find_referencing_symbols` for callers — grep has no equivalent; "
        "`get_symbols_overview` before opening a file whole; `find_symbol` instead of "
        "`grep -rn \"def foo\"`.",
        "- `activate_project` on the repo root if a call says no project is active.",
    ]
    # Spec 2026-10-02-serena-the-cheap-path.md §4
    if tool_search == "off":
        return [
            "# Finding code: your symbol tools are IN your tool list",
            "They are listed with full schemas — `find_symbol`, "
            "`find_referencing_symbols`, `get_symbols_overview` and `activate_project` "
            "are callable directly, with no lookup call to make first.",
            *tail,
            "",
            *_MARKDOWN_NAV,
        ]
    return [
        "# Finding code: your symbol tools are DEFERRED, not absent",
        "They are NOT in your tool list — this CLI resolves them on demand. One "
        "`ToolSearch` call with this select brings back the four that matter (both "
        "prefixes; a name this install lacks is ignored):",
        NAV_SELECT_LINE,
        *tail,
        "",
        *_MARKDOWN_NAV,
    ]


def navigation_section(serena: bool = True, tool_search: str = "cli") -> str:
    """Serena before grep, for every session Jarvis dispatches"""
    if not serena:
        return "\n".join([
            "# Navigating the code: Glob and Grep",
            "This project does not wire Serena to its workers (`jarvis config wiring "
            "<project>` says so), so there are no symbol tools in your tool list and "
            "nothing to activate. `Glob` and `Grep` are the tools for finding code "
            "here, and there is nothing to apologise for — just expect to work harder "
            "for a less complete picture than a symbol index would give.",
            "",
            "- Search for the DEFINITION spelling, not the mention: `grep -rn \"def "
            "foo\"` before `grep -rn foo`, and widen only when it misses.",
            "- Callers are the expensive question here. Grep the bare name, then read "
            "each hit — a caller that spells it differently (an alias, a getattr, a "
            "string in a table) is the one text search cannot find, so say what your "
            "sweep would have missed rather than claiming it was exhaustive.",
            "- Read a file's imports and its module docstring before its body: on a "
            "project with a written map, the map is cheaper than the code.",
            "",
            # Spec 2026-10-06-navigate-specs-like-code.md §6
            *_MARKDOWN_NAV,
        ])
    lines = [
        "# Navigating the code: Serena first, grep second",
        # §3 item 3 of docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md
        ("Serena is wired to this project, and its symbol tools are listed with full "
         "schemas — so use them to find code and do NOT grep for symbols."
         if tool_search == "off" else
         "Serena is wired to this project, and its symbol tools are DEFERRED behind "
         "`ToolSearch` rather than listed — so use them to find code and do NOT "
         "grep for symbols.") + " Serena has a language-server symbol index, so "
        "`find_symbol`, `get_symbols_overview` and especially "
        "`find_referencing_symbols` answer where something is defined and who "
        "calls it as facts, in one call. Grep answers a different question — where "
        "a string appears — and you then have to rebuild the answer from hits that "
        "miss every caller spelling the name differently.",
        "",
        "- `list_memories` / `read_memory` FIRST on a mapped project: its "
        "architecture is already written down, and rediscovering it is the most "
        "expensive thing you can do with your context.",
        "- `find_symbol` instead of `grep -rn \"def foo\"`; "
        "`find_referencing_symbols` instead of grepping for call sites — that one "
        "has no grep equivalent; `get_symbols_overview` before opening a file "
        "whole.",
        "- `search_for_pattern` (Serena's own) or `Grep` for GENUINE text "
        "questions: a config key, an error string, a TODO. Text search is not "
        "wrong, it is just the wrong tool for finding code.",
        "- If the symbol tools say no project is active, `activate_project` on the "
        "repo root first.",
        "",
        # Spec 2026-10-06-navigate-specs-like-code.md §6
        *_MARKDOWN_NAV,
    ]
    return "\n".join(lines)


def concision_section() -> str:
    """The three surfaces this OS is verbose on, and what each is allowed to hold (docs/superpowers/specs/2026-08-22-agent-concision.md)"""
    lines = [
        "# Point, do not explain",
        "Three surfaces, one rule each. The HOUSE STYLE governs how you write all "
        "three and everything else — it is injected into every turn of your session "
        "and its rules are your `caveman` and `ponytail` skills "
        "(docs/superpowers/specs/2026-09-19-concision-enforced.md). This section is "
        "what the style cannot know: which surface holds what.",
        "",
        "## Code comments point at documentation; they are not documentation",
        "One line, naming the reason or citing a spec section: `# -- SS3`. If the "
        "explanation runs past a couple of lines it belongs in "
        "`docs/superpowers/specs/YYYY-MM-DD-<name>.md` with numbered sections, and "
        "the comment cites it by number. Write that spec as part of the work when "
        "there is none. This is kn-f861a2f6, from a review that took 206 lines of "
        "comment out of `src/` and put 70 back: the reasoning was wanted, the "
        "place was wrong.",
        "",
        "## A PR body hints to the reviewer; it does not explain the feature",
        "What to look at first, what is risky, what was decided and where the "
        "reasoning lives. The reviewer has the diff and does not need it "
        "narrated. If the PR needs a rationale longer than a few lines, that "
        "rationale is a spec and the PR links to it.",
        "",
        "This cuts NARRATION OF THE DIFF, and nothing else. It is not licence to "
        "ship a thin body: the summary, the implementation notes, the Neo "
        "questions, the alarms this order raised, the learnings, the test "
        "evidence and the screenshots of anything you changed on a rendered "
        "surface all stay, because none of them is anywhere else. Fill the "
        "repository's PR template — your "
        "`open-a-pull-request` skill has it, and a `gh pr create` missing a "
        "section is denied "
        "(docs/superpowers/specs/2026-08-24-a-pull-request-a-reviewer-can-read.md).",
        "",
        "## Pass a REFERENCE, never a payload",
        "A diff, a log, a file or a JSON dump never goes into a `jarvis` command, a "
        "message or a question: pass a pull request URL, a commit SHA, a path with a "
        "line range or the command that reproduces it, because whoever reads it can "
        "run that and nobody needs it pasted — an oversized argument, and a `$(git "
        "diff …)` substitution, are refused before the command runs "
        "(docs/superpowers/specs/2026-09-26-bounded-model-inputs.md).",
        "",
        "## Say each thing once, across the whole record",
        "The description, your questions, your messages and your finish summary "
        "are read TOGETHER. A paragraph repeated across them is read three times "
        "and learned once. Refer to what is already on the record — 'as in the "
        "description', 'question 12 settled this' — instead of restating it. "
        "Questions especially: state the decision, the options and your "
        "recommendation, and do not paste context the reader already has.",
        "",
        "None of this trades against correctness. Failing test output, error "
        "messages, security caveats and the things you did NOT do keep their full "
        "content — the rule deletes restatement, never evidence.",
    ]
    return "\n".join(lines)


def gates_section(wo_id: str = WO_PLACEHOLDER,
                  enabled: tuple[str, ...] | None = None) -> str:
    """The full privileged-action protocol"""
    from .gates import KINDS

    live = [k for k in KINDS if k.name in enabled] if enabled else list(KINDS)
    lines = [
        "# Privileged actions (gated, NOT forbidden)",
        "These actions are reviewed before they run — an independent reviewer "
        "(Neo, the user's delegate) decides, and approval lets you proceed:",
    ]
    # Each kind is listed WITH the two questions its own request has to answer
    for k in live:
        lines += [
            f"- `{k.name}` — {k.summary}",
            f"    --why: {k.why_ask}",
            f"    --evidence: {k.evidence_ask}",
        ]
    if not enabled:
        lines += [
            "(All kinds are listed because no project context was given; your "
            "opening prompt names the ones live for your project.)",
        ]
    lines += [
        "",
        "ASK FIRST, BEFORE YOU ARE BLOCKED. Nothing has been filed yet, so the "
        "command string is the only handle there is — and this is the only command "
        "that starts a review, because the reviewer sees ONLY the text you write:",
        f"    jarvis gate request {wo_id} \"<the exact command>\" "
        f"--why \"<the --why your kind asks for, above>\" "
        f"--evidence \"<the --evidence your kind asks for, above>\"",
        "",
        "",
        "AFTER A BLOCK, USE THE REQUEST NUMBER THE BLOCK PRINTED — never the command "
        "string. The OS already has the string; re-typing it into an argument is how "
        "a worker in a git worktree finds its own way out refused by the isolation "
        "guard, which inspects arguments and rejects any text it cannot prove is not "
        "a git operation. None of these runs the command:",
        f"    jarvis gate request <request-number> --why \"...\" --evidence \"...\"",
        "    jarvis gate contest <request-number> --why \"...\"",
        "    jarvis gate explain <request-number>",
        "",
        "Attempting one directly is safe but it is NOT a request. The attempt is "
        "blocked and the OS records it, holding it back from review precisely "
        "because you have argued nothing yet — so nobody is looking at it, and "
        "retrying the command will not change that. Run the command above and it "
        "becomes the same request, now with your case; it is never a second one, "
        "because one action gets one review. Leave it unargued and the OS closes "
        "it as ABANDONED once its window runs out: nobody will have reviewed it, "
        "nothing will be decided, and the command stays blocked.",
        "",
        "Then END YOUR TURN. The verdict arrives as your next user turn. If "
        "approved, run that exact command — the approval is scoped to that one "
        "string and expires, so do not reword it. If denied, fix what the reason "
        "names; do not retry as-is.",
        "",
        "THE OTHER EXIT — the recogniser matches TEXT, so it sometimes fires on a "
        "command that merely NAMES one of these actions: a release script inside a "
        "grep pattern, a path quoted in a PR body, a heredoc that writes a file. "
        "That is an OS bug, not a refusal, and the command above is the wrong one "
        "for it — the command performs none of these actions, so every answer the "
        "request asks you for would be false. Contest the match instead:",
        "    jarvis gate contest <request-number> "
        "--why \"<why this performs no privileged action>\"",
        "",
        "That reaches the same reviewer as a candidate DISMISSAL rather than a "
        "request for permission. It authorises nothing and needs no evidence; the "
        "two outcomes are dismissed (you were right — run the command as written) "
        "and denied (the command really does perform the action, and your route "
        "back is a real request). If you cannot tell which of the two exits you "
        "need, ask the OS before you choose — it reports where the matched literal "
        "sits and whether the shell would run it:",
        "    jarvis gate explain <request-number>",
        "",
        "Contesting is worth more than walking away. A dismissal TEACHES the "
        "recogniser: the OS derives a standing rule from the shape of what was "
        "wrongly matched, so the next worker writing something similar — in this "
        "project or any other — is never blocked at all. Rewording the command to "
        "dodge the gate, or abandoning it and working around the block, teaches it "
        "nothing and leaves the defect in place for everyone else.",
        "",
        "A dismissal clears a command STRING; it does not reset the review state "
        "of the action that string talks about. So NEVER open a second request for "
        "a privileged action while an equivalent one is still pending or escalated "
        "— a dismissal is not permission to re-file. If the first request stalled "
        "because it was missing evidence, send that evidence to the reviewer "
        "(`jarvis wo ask`, or `jarvis notify` if the user has to see it) and leave "
        "the original standing.",
    ]
    return "\n".join(lines)


def knowledge_section(project: str = PROJECT_PLACEHOLDER) -> str:
    """How to use the knowledge base — both halves, in full"""
    lines = [
        "# The OS knowledge base",
        "It is the fleet's only durable memory, and your prompt carries an INDEX "
        "of it (headline + id), never the entries themselves — when the base is "
        "empty there is no index and nothing to read yet.",
        "",
        "READ on demand — look up any area you are about to touch BEFORE you "
        "touch it, and before you ask or assume about it; a past worker probably "
        "already paid for the lesson:",
        "```bash",
        f'jarvis learn search "<term>" --project {project}  # which entries match: '
        f"headline, id, one matching line",
        "jarvis learn show <id> [<id> ...]  # full text of specific entries — the only "
        "verb that returns a body",
        f"jarvis learn list --project {project} --topic <t>  # everything in a "
        f"topic",
        f"jarvis learn topics --project {project}  # what topics exist",
        "```",
        "A headline is a truncated first line and an excerpt is one quoted matching "
        "line, never the whole entry: if it looks relevant, `show` it rather than "
        "acting on the summary. And LOOK IT UP "
        "FIRST when a headline names the area you are unsure about — a lookup is "
        "not a doubt, so it comes before `jarvis wo ask`. When nothing in the "
        "index fits, ask; never let searching become a substitute for recording a "
        "call you made with no doubt.",
        "",
        "WRITE back what you learn — it is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project} --topic \"<topic>\"`. "
        "Anything durable — project state, gotchas, conventions, decisions — goes "
        "there. Your own memory files, notes and scratch docs are invisible to "
        "the user, to Neo and to the next worker.",
    ]
    return "\n".join(lines)
