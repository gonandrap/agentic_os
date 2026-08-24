"""What a worker is told, split into a minimal core and full briefings on demand.

The opening prompt used to carry the whole ~8KB operating contract — every worker
paying for the gate protocol, the navigation posture and the finishing prose whether
or not that territory ever came up in its session. This module is the single source
for the replacement: a compressed core of only the load-bearing invariants
(`core_contract`), an index of the full sections (`section_index`), and the sections
themselves, fetched with `jarvis brief <section>` — the same map-plus-retrieval-verb
pattern the knowledge base already uses on these same workers.

Single-sourced on purpose: `dispatch.build_worker_prompt` composes the core from here
and `cli.cmd_brief` renders the full sections from here, so there is no second copy
to drift. The planner's prompt is NOT split (one session per feature, already
reviewed as a unit); it still borrows `navigation_section` and `gates_section` in
full via dispatch's `_common_briefing`.

Two invariants inherited from the knowledge base work:

* kn-97c41de7 — the prompt never points at a resource that may not exist. Sections
  are static text, so `jarvis brief` always answers; the `gates` hook only appears
  for projects with gates enabled, and the knowledge read bullet stays conditional
  on a non-empty knowledge base, exactly as before the split.
* kn-ea760e6e — a shrunk prompt ships with a same-input A/B eval:
  evals/llm/test_worker_contract_ab.py runs the old and new composition of the same
  scenarios against the real model. tests/test_worker_brief.py holds the free
  harness checks CI always runs.
"""

from __future__ import annotations

WO_PLACEHOLDER = "<wo-id>"
PROJECT_PLACEHOLDER = "<project>"

#: Budget for the rendered core contract block (between "# Operating contract" and
#: "# Full briefings"), excluding the work order's own description and the knowledge
#: index. Enforced by tests/test_worker_brief.py.
#:
#: Raised from 2500 in wo-322cd6f8: the original figure was set against a 2080-char
#: core, and PR 124's `--evidence` bullet spent all of that headroom. A bullet earns
#: its place here only if a worker that lacks it does damage BEFORE it would think to
#: fetch the section — which is why concision is here rather than fetch-only
#: (docs/superpowers/specs/2026-08-22-agent-concision.md SS6). Raise it again only on
#: that test, not to make room.
CORE_BUDGET_CHARS = 2750


# -- the git briefing, replacing Claude Code's own ---------------------------------------

# Model id -> the name Claude Code puts in the Co-Authored-By trailer, copied from the
# CLI's own table (2.1.233) so a Jarvis-written commit is indistinguishable from one
# written under the built-in instructions. Longest-prefix matched, so a dated id like
# `claude-haiku-4-5-20251001` resolves; anything unrecognised — including a floating
# alias such as `opus`, whose target only the CLI knows — falls back to the CLI's own
# fallback, plain "Claude". A generic trailer is correct; a wrong model name is not.
MODEL_ATTRIBUTION_NAMES: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-mythos-5": "Claude Mythos 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
}

#: The PR footer, verbatim from the CLI bundle.
PR_ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def attribution_name(model: str | None) -> str:
    """The display name for the Co-Authored-By trailer."""
    m = (model or "").strip().lower()
    hit = [k for k in MODEL_ATTRIBUTION_NAMES if m.startswith(k)]
    return MODEL_ATTRIBUTION_NAMES[max(hit, key=len)] if hit else "Claude"


def git_briefing(model: str | None = None) -> str:
    """The git conventions, as a STATIC block Jarvis owns.

    Workers run with `includeGitInstructions: false`, which switches off three things
    the CLI would otherwise build into the system prompt: the git-status snapshot, the
    long "Committing changes with git" workflow, and the lean "# Git" block carrying
    the attribution trailers. The snapshot is the reason — it is recomputed from the
    working tree at every `--resume`, so it changed the moment the worker edited a
    file, and a changed system prompt invalidates the cached prefix for the ENTIRE
    conversation. Measured on 2.1.233: turn 2 of a resumed worker wrote 10,983 tokens
    and read 15,995 (the static prompt alone) with the snapshot on, against 552 written
    and 26,113 read with it off. See docs/superpowers/specs/
    2026-08-15-a-stable-prefix-for-resumed-workers.md.

    The other two blocks are collateral, so this restates them — which is the whole
    trick, and the linked best-practice rule: the same content, from a surface that
    cannot move between turns. It is parameterised only by the work order's model,
    which is frozen at dispatch (and is part of the cache key anyway), so the rendered
    text is byte-identical for every turn of a given work order.

    Deliberately NOT a copy of the CLI's "commit only when the user asks": for a worker
    the work order IS the ask, and the operating contract already tells it to commit and
    open a PR. Copying that line verbatim would have the system prompt contradict the
    contract.
    """
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
    """Asked for a briefing section that does not exist."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"unknown briefing section {name!r} — valid sections: "
            + ", ".join(SECTION_HOOKS))

    def __str__(self) -> str:  # KeyError.__str__ would repr-quote the message
        return self.args[0]


# One line per section, mirrored into the core's index. The hook has to sell the
# fetch: it is the only thing a worker knows about the section before paying for it.
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
                   gates_enabled: tuple[str, ...] | None = None) -> str:
    """One full section, rendered with real ids when the caller has them.

    Read-only and total: every listed name renders non-empty text with placeholders
    for anything unknown, because a worker mid-session must never fetch a briefing
    and get an error back (kn-97c41de7, applied to the OS's own sections).
    """
    wo = wo_id or WO_PLACEHOLDER
    proj = project or PROJECT_PLACEHOLDER
    if name == "contract":
        return contract_section(wo, proj)
    if name == "gates":
        return gates_section(wo, enabled=gates_enabled)
    if name == "record":
        return record_section(wo)
    if name == "navigation":
        return navigation_section()
    if name == "concision":
        return concision_section()
    if name == "knowledge":
        return knowledge_section(proj)
    raise UnknownSection(name)


# -- the core ---------------------------------------------------------------------------

def core_contract(wo_id: str, title: str, project: str, has_knowledge: bool,
                  gate_names: tuple[str, ...] = ()) -> list[str]:
    """The compressed operating contract: only the invariants a worker cannot be
    allowed to discover by fetching.

    Every sentence here survived the LLM evals that shaped the full contract
    (evals/llm/test_worker_judgment.py and the A/B in test_worker_contract_ab.py):
    the ask/assume routing, the doubt trigger, the named rationalisations and the
    last-message-is-the-record rule are behaviourally load-bearing and stay in the
    opening prompt. Everything explanatory moved behind `jarvis brief`.
    """
    lines = [
        "# Operating contract",
        "You MUST follow it. This is the compressed core; the full contract with "
        "all its reasoning is one read-only command away — see \"Full briefings on "
        "demand\" below.",
        "- Work only inside your assigned worktree (you start in it). Commit your "
        "work and open a PR per this repo's conventions. Never push to main.",
        f"- **The PR title MUST start with `[{wo_id}] `** — e.g. "
        f"`[{wo_id}] {title[:40]}`. `gh pr create` with any other title is blocked.",
        f"- **Neo is your first responder. Any doubt goes to it.** "
        f"`jarvis wo ask {wo_id} \"<your question>\"`, then END YOUR TURN; the "
        f"answer arrives as your next user turn, usually within a minute. This is "
        f"the normal, expected way to work: it is not an escalation and it does not "
        f"interrupt the user. The trigger is DOUBT, not importance — if you catch "
        f"yourself weighing options or thinking \"either would work\", you are in "
        f"doubt: Ask BEFORE you build on it, not after. A question is one "
        f"paragraph: the decision, the options, your recommendation — do not paste "
        f"context.",
        "  - Do not talk yourself out of asking: \"It's reversible\", \"it's only "
        "an implementation detail\", \"I'll note it as an assumption\" are "
        "rationalisations for guessing.",
        *([
            "  - But LOOK IT UP FIRST when a headline in the knowledge-base index "
            "below names the area you are unsure about — a lookup is not a doubt.",
        ] if has_knowledge else []),
        f"- `jarvis wo assume {wo_id} \"...\"` is for the OTHER case, and it should "
        f"be RARE: a call you made with NO doubt. Record EVERY such call, including "
        f"the small and obvious ones — the work order record is the only audit "
        f"trail anyone gets. Never a guess: if you are guessing, ask.",
        *([
            f"- READ the OS knowledge base before you touch an area it covers — it "
            f"is INDEXED at the end of this prompt, not pasted into it: "
            f"`jarvis learn show <id>`, `jarvis learn search \"<term>\" "
            f"--project {project}`.",
        ] if has_knowledge else []),
        f"- The OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project} --topic \"<topic>\"`. Your "
        f"own notes and memory files are invisible to everyone else.",
        *([
            f"- Privileged actions ({', '.join(gate_names)}) are gated, NOT "
            f"forbidden: an attempt is blocked, filed for review, and the gate's "
            f"full instructions arrive when one fires. You make a stronger case by "
            f"asking first — `jarvis gate request {wo_id} \"<the exact command>\" "
            f"--why \"<why this is ready>\" --evidence \"<PR, tests, checks>\"` "
            f"(full protocol: `jarvis brief gates --wo {wo_id}`).",
        ] if gate_names else []),
        f"- **Point, do not explain; say each thing ONCE.** A code comment is one "
        f"line citing the spec that explains it (`docs/superpowers/specs/`), not "
        f"the explanation. A PR body hints; the diff explains. Never restate what "
        f"is already on this record. Rules: `jarvis brief concision`.",
        f"- When done, ALWAYS run `jarvis wo finish {wo_id} --summary \"...\"` "
        f"(add `--pr <url>` if you opened a pull request) and then write your "
        f"complete answer as the last thing you say. The work order record IS this "
        f"conversation: the last message of every turn is captured verbatim, the "
        f"user and Neo decide from that record, and neither will ever open this "
        f"session — a detail that lives only in the summary is a detail that "
        f"ceases to exist.",
        f"- Add `--evidence \"<what you ran and what it showed>\"` to that same "
        f"finish: the tests, evals and checks you actually ran, and what they "
        f"reported. The summary says what you built; the evidence says how you "
        f"know it works, and it is read beside your diff. Review feedback may come "
        f"back asking for more — do what it asks, then finish again with the "
        f"fuller account.",
    ]
    return lines


def section_index(wo_id: str, gated: bool) -> list[str]:
    """The map of the full sections, mirroring the knowledge-index pattern."""
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
        lines.append(f"- `{name}` — {hook}")
    return lines


# -- the full sections ------------------------------------------------------------------

def _question_shape(wo_id: str) -> list[str]:
    """The full question protocol, shared by the contract section (single source for
    the sentences the question-diet tests pin)."""
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
        f"design doc \"docs/specs/feature.md\": …`) that section is delivered "
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
    """The complete operating contract — the pre-split text, moved rather than
    lost. tests/test_worker_brief.py pins its load-bearing phrases one by one."""
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
        f"- `jarvis wo assume {wo_id} \"...\"` is for the OTHER case, and it "
        f"should be RARE: a call you made with NO doubt — you followed an existing "
        f"convention, the work order implied it, the codebase left one sensible "
        f"option. Record EVERY such call, including the small and obvious ones "
        f"(naming, file layout, which convention you followed, how you split the "
        f"commits): recording is cheap and the work order record is the only audit "
        f"trail anyone gets. An assumption is a disclosure of something you were "
        f"SURE about. It is never a guess you are hoping nobody checks — if you "
        f"are guessing, ask instead.",
        f"- Found work that is real but is not THIS work order's job? Do not file "
        f"it yourself and do not leave a note: `jarvis wo defer {wo_id} \"<title>\" "
        f"--why \"<why it should not be done now>\"` (add `--neo-question <id>` if "
        f"you agreed the deferral with Neo, `-d` to brief whoever picks it up). That "
        f"is the whole action — the OS decides where it lands, records which work "
        f"order suggested it, and tells you nothing back, because nothing you do "
        f"next should depend on the answer.",
        f"- READ the OS knowledge base on demand: `jarvis learn show <id>` for an "
        f"entry your prompt's index lists, `jarvis learn search \"<term>\" "
        f"--project {project}` to sweep for one. Look up any area you are about to "
        f"touch BEFORE you touch it, and before you ask or assume about it — a "
        f"past worker probably already paid for the lesson. A headline is a "
        f"truncated first line, never the whole entry: if it looks relevant, fetch "
        f"it rather than acting on the summary.",
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
        f"a pull request, pass it too: `--pr <url>`. The full finishing protocol "
        f"and what the record demands of every turn: `jarvis brief record --wo "
        f"{wo_id}`.",
        "",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise. User feedback may arrive as new user turns; treat "
        "it as authoritative for this work order.",
    ]
    return "\n".join(lines)


def record_section(wo_id: str = WO_PLACEHOLDER) -> str:
    """What the outside world sees, plus the finishing protocol in full."""
    lines = [
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is "
        "concerned. The last message of every turn you take is captured verbatim "
        "into it, and the user and Neo make their decisions from that record — "
        "neither will ever open this session. So end every turn with the complete "
        "answer: findings, caveats, uncertainties, what you did NOT do, and "
        "absolute paths. `--summary` is a one-line headline for that answer, never "
        "a substitute for it — anything that lives only in the summary is the only "
        "thing anyone reads, so a detail you drop there is a detail that ceases to "
        "exist.",
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
        "# Evidence",
        f"Pass `--evidence \"<what you ran and what it showed>\"` to `jarvis wo "
        f"finish` as well: the tests, evals and manual checks you actually "
        f"performed, and what they reported. It is a different question from "
        f"`--summary`. The summary says what you built and where; the evidence "
        f"says how you know it works, and it is read beside your diff by someone "
        f"who was not in this session and takes nothing on trust. \"Ran the "
        f"suite\" is not evidence; \"`uv run pytest -q` — 412 passed, 0 failed, "
        f"including the 6 new cases in tests/test_thing.py\" is.",
        "",
        "Review feedback may come back asking for more: a case you did not cover, "
        "a claim the diff does not support, a check you described but did not "
        "run. That is an ordinary part of finishing. Do the work it asks for, "
        "then run `jarvis wo finish` again with the fuller account — a second "
        "`finish` that only rewords the first one has added nothing.",
        "",
        "Do not stop without finishing: a session that ends with neither "
        f"`jarvis wo finish` nor an open question is invisible work.",
    ]
    return "\n".join(lines)


def navigation_section() -> str:
    """Serena before grep, for every session Jarvis dispatches.

    Prose rather than a capability restriction, and it has to be: a worker needs
    `Grep` and `Bash` for its actual job, so the seats' trick of simply not
    granting the tool is not available. Stated conditionally because Jarvis knows
    nothing about Serena — whether a worker has it depends on the user's own Claude
    configuration and on whether the project is indexed.
    """
    lines = [
        "# Navigating the code: Serena first, grep second",
        "If this project has Serena (its symbol tools appear in your tool list, or "
        "`.serena/project.yml` is in the repo), use it to find code and do NOT "
        "grep for symbols. Serena has a language-server symbol index, so "
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
        "If the project has no Serena, `Glob` and `Grep` are the fallback and "
        "there is nothing to apologise for — just expect to work harder for a less "
        "complete picture.",
    ]
    return "\n".join(lines)


def concision_section() -> str:
    """The three surfaces this OS is verbose on, and what each is allowed to hold.

    Design and the probe behind the output-style half:
    docs/superpowers/specs/2026-08-22-agent-concision.md — this section is SS6.
    """
    lines = [
        "# Point, do not explain",
        "Three surfaces, one rule each. All three are things you WRITE, which is "
        "why no output style covers them — the OS sets `outputStyle: Concise` for "
        "what you SAY, and this section is the rest.",
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
        "questions, the learnings and the test evidence all stay, because none of "
        "them is anywhere else. Fill the repository's PR template — your "
        "`open-a-pull-request` skill has it, and a `gh pr create` missing a "
        "section is denied "
        "(docs/superpowers/specs/2026-08-24-a-pull-request-a-reviewer-can-read.md).",
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
    """The full privileged-action protocol.

    With `enabled` (the project's live gates, as dispatch and the worker's
    JARVIS_GATES env know them) only those are listed, exactly as the old briefing
    did; without project context every kind is described, with a note, because the
    section must always render something true.
    """
    from .gates import KINDS

    live = [k for k in KINDS if k.name in enabled] if enabled else list(KINDS)
    lines = [
        "# Privileged actions (gated, NOT forbidden)",
        "These actions are reviewed before they run — an independent reviewer "
        "(Neo, the user's delegate) decides, and approval lets you proceed:",
    ]
    lines += [f"- `{k.name}` — {k.summary}" for k in live]
    if not enabled:
        lines += [
            "(All kinds are listed because no project context was given; your "
            "opening prompt names the ones live for your project.)",
        ]
    lines += [
        "",
        "Attempting one directly is safe: the attempt is blocked, a request is "
        "filed automatically, and you are told to wait. But you make a much "
        "stronger case by asking first, because the reviewer sees ONLY the text "
        "you write:",
        f"    jarvis gate request {wo_id} \"<the exact command>\" "
        f"--why \"<why this is ready>\" --evidence \"<PR number, test results, "
        f"checks>\"",
        "",
        "Then END YOUR TURN. The verdict arrives as your next user turn. If "
        "approved, run that exact command — the approval is scoped to that one "
        "string and expires, so do not reword it. If denied, fix what the reason "
        "names; do not retry as-is.",
        "",
        "A third verdict exists: DISMISSED. The recogniser matches text, so it "
        "sometimes fires on a command that merely NAMES one of these actions — a "
        "release script inside a grep pattern, a path quoted in a PR body. That is "
        "an OS bug, not a refusal: the reviewer dismisses it, nothing is "
        "authorised, and you may run the command as written. So if a gate fires on "
        "something you know ships nothing, do not reword the command to get around "
        "it — file it, say plainly why it performs no privileged action, and end "
        "your turn.",
        "",
        "Filing it is worth more than it used to be. A dismissal now TEACHES the "
        "recogniser: the OS derives a standing rule from the shape of what was "
        "wrongly matched, so the next worker writing something similar — in this "
        "project or any other — is never blocked at all. Rewording to dodge the "
        "gate teaches it nothing and leaves the defect in place for everyone else. "
        "To see why a command was matched before you file, run `jarvis gate "
        "explain \"<the exact command>\"`.",
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
    """How to use the knowledge base — both halves, in full."""
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
        f'jarvis learn search "<term>" --project {project}  # full text of matches',
        "jarvis learn show <id> [<id> ...]  # full text of specific entries",
        f"jarvis learn list --project {project} --topic <t>  # everything in a "
        f"topic",
        f"jarvis learn topics --project {project}  # what topics exist",
        "```",
        "A headline is a truncated first line, never the whole entry: if it looks "
        "relevant, fetch it rather than acting on the summary. And LOOK IT UP "
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
