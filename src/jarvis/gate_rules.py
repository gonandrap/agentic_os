"""The rule base behind the privileged-action gates — and how it learns.

`gates.py` owns the *lifecycle* of a privileged action: file a request, get a verdict,
open the gate. This module owns the prior question — **is this command privileged at
all?** — and, unlike the lifecycle, that question has an answer that changes over time.

## Why this is a table and not a constant

The recognisers used to be regex tuples in `gates.KINDS`. Every false positive they
produced was reviewed by Neo, correctly identified as a false positive, dismissed — and
then forgotten. The next work order, in the next project, writing the same shape of
commit message, tripped the same gate and spent another review on it. Four fired on one
work order alone (gates 40-43 on wo-f49dab38); the fourth blocked the commit of the fix
for the third. The only route from "Neo knows this shape is harmless" to "the OS stops
gating this shape" ran through a human filing a work order to widen a regex.

That is the loop this module closes. The rules live in `os.db` (central, so a dismissal
in one project settles the question for the next), they are SEEDED from the constants
that used to be the whole story, and they GROW from Neo's dismissal verdicts. Nothing
reads the seeds at classification time — `RuleSet.load()` reads the table, and the seeds
are consulted in exactly two places: when the table is first written, and as a fallback
when the database cannot be read at all (see `RuleSet.from_seeds`, and note which
direction that fallback errs in).

## The three roles

A rule is one of three things, and the third is what makes the first two safe to change:

- `match` — a recogniser. "A command matching this regex attempts this gated action."
- `exempt` — a clearance. "A command of this shape only *mentions* the action."
- `canary` — a command that MUST always gate. Not consulted during classification: it is
  the test every proposed exemption has to pass before it is allowed to exist.

Canaries encode the settled convention the reviewer persona states in prose: a command
that actually invokes the deploy script, actually merges a pull request, or actually
restarts a service can never be cleared, however it was argued for. Encoded here, that
rule survives contact with a mistaken reviewer, a sloppy learned pattern and a future
edit to the seeds — `check_canaries()` re-derives it from the live table, and
`INV-GATE-CANARY` runs it every reconcile tick.

## How a dismissal becomes a rule

Two mechanisms, layered, in this order:

1. **The reviewer's own generalisation.** Neo may return an `exempt_pattern` alongside a
   `dismiss` verdict — a regex describing the family the command belongs to. It is
   validated (it must compile, it must contain a literal anchor, and it must actually
   match the command it was written for) and then canary-tested.
2. **A structural signature**, derived mechanically, used when Neo proposes nothing or
   proposes something that fails validation. This is the floor, and it cannot be argued
   with because no model authors it: the OS records *where in the command the gated
   literal appeared* (`heredoc` body, `quoted` span) and *which command owns that
   position* (`git commit`), and clears future commands of that same shape.

The signature deliberately cannot express "in executable position". A match at `code`
position is text the shell will run, and no amount of learning may clear it. Nor does a
signature apply where an executor (`bash`, `eval`, `xargs`, `python`) shares a command
with the literal: `git commit <<EOF … shipit … EOF` is a commit message, `cat <<EOF |
bash … shipit … EOF` is a release, and the difference between them is exactly that.

## The unit all of that is judged over

Not the whole command string: the independent command inside it. `||`, `&&`, `;` and `&`
join commands that share no STREAM, so an executor on one side of one usually says
nothing about the other — `cat …/shipit/SKILL.md || find ~ -name SKILL.md` is two reads,
and judging it whole made it a release (issue #194). A PIPE is the opposite and stays
whole, because `cat scripts/shipit.sh | bash` really does ship; so do substitutions and
heredoc bodies, which belong to the command that reads them. `list_segments` draws that
line once and `read_only_mentions` and `Shape` both work over it.

"Belong to" is load-bearing and was not true until issue #233. A heredoc body sits on its
own lines, and a newline is a list separator, so every body used to split off into a
segment of its own that no command owned: `cat > /tmp/why.txt <<EOF` owned nothing, the
prose inside it was judged as if someone had typed it at a prompt, and the gated literal
it merely quoted was charged to whatever ran next. `owned_spans` is where that is fixed —
one span per heredoc, from the newline ending its opener line to the end of its
terminator line, blanked with the body so the whole thing stays with its owner.

"Usually" is load-bearing, and getting it wrong is worse here than anywhere else in this
module. Those segments DO share the filesystem and the environment, so a reader that
writes — `cat scripts/shipit.sh > /tmp/s.sh && bash /tmp/s.sh` — hands its subject to
the next command by a route no parser here follows. `_hands_off` spots the handing over
and puts the whole chain back under the chain-wide test. The asymmetry is on purpose:
issue #194 was a gate that fired when it should not, and the only thing worse than that
is one that stays quiet when it should.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .central_store import CentralStore

# -- vocabulary -----------------------------------------------------------------------

MATCH = "match"
EXEMPT = "exempt"
CANARY = "canary"
ROLES = (MATCH, EXEMPT, CANARY)

# How a rule's `pattern` column is interpreted.
REGEX = "regex"          # a regular expression
SIGNATURE = "signature"  # JSON: a structural shape, see `Shape`
COMMAND = "command"      # a literal command string (canaries only)
TESTS = (REGEX, SIGNATURE, COMMAND)

# Where in a command a gated literal turned up. Only the first is executable, and only
# the other two can ever be exempted.
CODE = "code"
HEREDOC = "heredoc"
QUOTED = "quoted"
EXEMPTIBLE_POSITIONS = (HEREDOC, QUOTED)

SOURCES = ("builtin", "neo", "user")

#: The gate kind the OS files against ITSELF. Named here rather than spelled at the six
#: call sites that branch on it, because a typo in one of them silently restores the
#: worker-messaging path this kind exists to switch off (§5).
SELF_HEAL = "self_heal"


# -- what a gate IS (metadata; the patterns live in the table) ------------------------


@dataclass(frozen=True)
class GateKind:
    """One class of privileged action.

    Note what is *not* here any more: the patterns. A kind is now the thing a reviewer
    and a worker need to talk about — a name and a consequence — while what counts as an
    attempt at it is data in `gate_rules`, because that is the part that has to change
    without a release.
    """

    name: str
    # What the action does, in the terms whoever reviews it needs. Rendered into the
    # request Neo sees, so it must read as a claim about consequences.
    summary: str
    # WHAT TO ASK THE WORKER FOR, for this kind. Every surface that tells a worker how to
    # file a request renders these two — the hook's block, the CLI help, the standing
    # brief — and the reviewer's page labels the evidence box with the second.
    #
    # They are here rather than written out at each site because the sites were all
    # hardcoded to the RELEASE frame: "why this is ready to ship", "PR, tests, checks".
    # A worker asked for a PR number before restarting a service supplies the wrong
    # thing, and the reviewer then decides on the wrong thing — the request text is all
    # it ever sees. Nothing is "ready to ship" about a `jarvis config set`.
    why_ask: str = "why this action should proceed"
    evidence_ask: str = "what a reviewer can check"
    # Literals that, appearing in a project's `permissions.deny` rules, mean the deny
    # will shadow this gate — the call is blocked before the hook's `allow` is even
    # consulted, so approval can never take effect. Listed explicitly rather than
    # derived from the patterns, because deriving them turns common words like "start"
    # into false alarms on rules like `Bash(npm start*)`.
    conflict_markers: tuple[str, ...] = ()


KINDS: tuple[GateKind, ...] = (
    GateKind(
        name="pr_merge",
        summary="merge a pull request into the default branch",
        why_ask="why this is ready to merge",
        evidence_ask="the PR number, its review state, and what its checks report",
        conflict_markers=("gh pr merge", "pulls/"),
    ),
    GateKind(
        name="release",
        summary="cut a release and deploy it (this reaches the live production fleet)",
        why_ask="why this is ready to ship",
        evidence_ask="the merged PRs, the commits being tagged, and CI's verdict on them",
        conflict_markers=("shipit", "gh release", "npm publish", "twine publish",
                          "uv publish", "--tags", "--follow-tags"),
    ),
    GateKind(
        name="service_restart",
        summary="restart or stop a system service (this interrupts the running fleet)",
        why_ask="why this service has to be interrupted, and why now",
        evidence_ask="which service, what is running on it right now, and what that "
                     "work loses when it bounces",
        conflict_markers=("systemctl",),
    ),
    GateKind(
        name="push_protected",
        summary="push directly to a protected branch, bypassing review",
        why_ask="why this cannot go through a pull request",
        evidence_ask="the branch, exactly what the push contains, and who agreed to "
                     "skip review",
        conflict_markers=("git push",),
    ),
    GateKind(
        name="config_write",
        summary="change the fleet's own configuration (this can turn gates, validation "
                "and worker permission modes off)",
        why_ask="why this setting has to change",
        evidence_ask="the setting, its value now, the value after, and what the change "
                     "switches off",
        conflict_markers=("jarvis config",),
    ),
    # THE ONLY KIND NOTHING CLASSIFIES INTO, and the only one a project cannot switch
    # off. Every kind above exists to catch a command a WORKER typed, so it has
    # recognisers and rides `GateConfig.enabled`; this one is filed programmatically by
    # `remedies.propose` when the supervisor wants to act on a unit it judged unhealthy,
    # and it is the only thing standing between a health judgement and a running
    # session — so `remedies.propose` never consults `GateConfig` and there is nothing
    # to disable. What a project controls instead is `supervisor.remedies.allowed`,
    # which is the other direction: what may be ASKED for, not whether asking is
    # required. Deliberately no `SEED_MATCHES` and no `SEED_CANARIES` entry, and
    # `gates.classify` therefore returns it for nothing —
    # docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.
    #
    # No `conflict_markers` for the same reason: `deny_conflicts` asks whether a
    # project's `permissions.deny` rules would shadow a gate, and nothing this gate
    # authorises is ever run through a shell.
    GateKind(
        name=SELF_HEAL,
        summary="let the supervisor act on a work order or feature order it judged "
                "unhealthy (this reaches a running session)",
        # No worker ever fills these in — `remedies.propose` writes the request itself —
        # but they are the labels the reviewer's page uses, and a page headed "PR, tests,
        # checks" over a health finding is the same defect one level along.
        why_ask="why acting is better than leaving the symptom with the user",
        evidence_ask="the health finding, the probe that raised it, and what the remedy "
                     "touches",
        conflict_markers=(),
    ),
)

KIND_NAMES = tuple(k.name for k in KINDS)
KIND_ORDER = {name: i for i, name in enumerate(KIND_NAMES)}


# -- reading a command --------------------------------------------------------------

# Quoted spans are data, not code. `'…'` is literal; `"…"` may interpolate, but a gated
# verb inside it is still an argument, not a command — unless a shell re-parses it, which
# is what _SHELL_INVOKER catches below.
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

# …with one exception: these hand their quoted payload back to a shell to execute, so
# there the quotes are code after all. Scan such commands whole.
_SHELL_INVOKER = re.compile(
    r"\b(?:ba|z|k|da|a)?sh\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*c\b|\beval\b|\bxargs\b",
    re.IGNORECASE,
)

# A heredoc opener: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`. `<<<word` is a here-STRING —
# one token on the same line, with no body — so it is excluded.
_HEREDOC_OPEN = re.compile(
    r"(?<!<)<<-?\s*(?:'(?P<sq>[^']+)'|\"(?P<dq>[^\"]+)\"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)

# Tools that read and cannot execute. A privileged action named in an *argument* to one
# of these is a mention, not an attempt: `cat scripts/shipit.sh` prints the release
# script, and printing it ships nothing.
#
# Membership is decided by what a tool CAN do, never by what it is usually used for.
# `find` has `-exec`, `awk` has `system()`, `xargs` and `eval` re-parse their input,
# `git` pushes, and `sed -i` writes — none of them are here or can be.
_READERS = frozenset({
    "cat", "tac", "head", "tail", "nl", "wc", "ls", "stat", "file", "diff", "cmp",
    "grep", "egrep", "fgrep", "rg", "ag", "cut", "sort", "uniq", "tr", "column",
    "jq", "yq", "basename", "dirname", "realpath", "readlink", "echo", "printf",
    "pwd", "tree", "strings", "od", "xxd", "md5sum", "sha256sum", "cksum", "du", "df",
    "which", "sed",
})

# The mirror image, and the one list a learned rule can never talk its way around: things
# that RUN text handed to them. A heredoc body is inert data to `git commit` and a
# program to `bash`, `python` or `awk`, so a structural exemption is void for any chain
# containing one of these — see `Shape.exemptible`.
#
# Same membership test as `_READERS`, applied the other way: what the tool CAN do. An
# interpreter that takes a script on stdin belongs here even when it is usually used
# interactively, because the exemption it would otherwise unlock is "the body of this
# heredoc is not code", and for these it always is.
_EXECUTORS = frozenset({
    "sh", "bash", "zsh", "ksh", "dash", "ash", "eval", "exec", "source", ".",
    "xargs", "python", "python3", "ruby", "perl", "node", "php", "awk", "gawk",
    "find", "make", "uv",
})

# Wrappers that run whatever follows them without changing what it can do, so the
# reader test applies to the word after them instead.
_TRANSPARENT = frozenset({"command", "builtin", "time", "nice", "ionice", "sudo", "env"})

# Tools whose first word says nothing useful: `git` is not a thing you do, `git push` is.
# Used only to make a signature specific enough to be worth having — an exemption learned
# from a commit message must not clear a push.
_SUBCOMMAND_TOOLS = frozenset({
    "git", "gh", "npm", "pnpm", "yarn", "uv", "pip", "poetry", "cargo", "docker",
    "systemctl", "jarvis", "kubectl", "brew", "apt", "gcloud", "aws",
})

# A substitution runs a command to build an argument, so the reader in front of it is
# no longer the only thing executing. `cat $(which shipit)` reads; `cat <(./shipit.sh)`
# ships. Neither is worth telling apart — both leave reader territory.
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")

# Where one command ends and the next begins. `&` splits only when it starts a
# background job: in `2>&1` it is part of a redirection, and splitting there would leave
# `2>` looking like a command name and fail every reader that redirects its stderr.
_SEPARATORS = re.compile(r"\|\||&&|[|;\n]|(?<![<>&])&")

# The subset of those that join INDEPENDENT commands — everything but the pipe. Nothing
# flows from one side to the other, so a judgement about one says nothing about the
# other, and a chain-wide answer over these is pure over-approximation (issue #194).
_LIST_SEPARATORS = re.compile(r"\|\||&&|[;\n]|(?<![<>&|])&(?!&)")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# What a redirection writes to. `>&2` and `2>&1` duplicate a file descriptor and
# `>/dev/null` discards — neither leaves anything behind. Any other target is a file the
# next command in the list can open, which is the one way a read hands its subject on.
_REDIRECT_TARGET = re.compile(r"&\d+|[^\s<>&|;]+")
_DISCARD = ("/dev/null",)

# Every entry of `_READERS` that can write a file anyway, with no redirection to give it
# away: `tee` takes a filename, `sort` has `-o`, `uniq` writes its SECOND positional
# argument, `yq` has `-i`, and `sed` has the `w` command — which lives inside a quoted
# script, so spotting it means parsing sed. Hence the blunt membership test: naming one
# of these is a handoff whether or not it wrote anything. It costs nothing unless the
# chain stops being all-readers, which is the only case where it matters.
_MAY_WRITE = frozenset({"tee", "sort", "uniq", "yq", "sed"})

# …and the subset of those whose write is an IN-PLACE edit of the file named. That is
# not a handoff, it is a modification of the gated target itself, so it costs the
# segment its reader status outright rather than merely arming `_hands_off`.
_INPLACE = frozenset({"sed", "yq"})


def heredoc_spans(command: str) -> list[tuple[int, int, int, bool]]:
    """`(body_start, body_end, opener_start, terminated)` for every heredoc in `command`.

    `terminated` is False when the delimiter line never turned up, so the body ran to the
    end of the string. Such a body is not a construct this parser understood, and
    `shape_of` refuses to call anything inside one prose — see the spec's §7.

    The body of a heredoc is the one span of a command that is neither quoted nor code:
    the shell reads it verbatim and hands it to the command as data. `_QUOTED` cannot see
    it — nothing about it is quoted — which is why `git commit -F -` with a message
    mentioning the release script was gated as a release (issue #104).

    Offsets are into the original string, so a caller can ask where a match landed.
    """
    spans: list[tuple[int, int, int, bool]] = []
    cursor = 0
    for m in _HEREDOC_OPEN.finditer(command):
        # An opener inside an earlier heredoc's body is text, not an opener.
        if any(s <= m.start() < e for s, e, _, _ in spans):
            continue
        delim = m.group("sq") or m.group("dq") or m.group("bare")
        # The body starts on the line after the opener — or, when a second heredoc opens
        # on the same line, after the first one's terminator.
        nl = command.find("\n", max(m.end(), cursor))
        if nl == -1:
            continue  # an opener with no body: nothing was ever read
        start = nl + 1
        end = len(command)
        terminated = False
        pos = start
        while pos <= len(command):
            eol = command.find("\n", pos)
            line = command[pos:eol if eol != -1 else len(command)]
            if line.strip() == delim:
                end = pos
                terminated = True
                break
            if eol == -1:
                break
            pos = eol + 1
        spans.append((start, end, m.start(), terminated))
        cursor = end
    return spans


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Replace `spans` with spaces, preserving length and newlines.

    Length-preserving because the caller is about to ask *where* something matched, and
    an offset into a string of a different length is worse than no answer.
    """
    out = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(out), e)):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def owned_spans(command: str) -> list[tuple[int, int]]:
    """The span each heredoc occupies in `command`, as text belonging to its OWNER.

    Wider than `heredoc_spans` at both ends, and each end closes a different way of
    charging a body to the wrong command (issue #233).

    It starts at the newline ENDING the opener line, not at the body: that newline is a
    list separator everywhere else in this module, so leaving it made every body a
    command of its own — `cat > f <<EOF` owned nothing, and the prose inside it was
    judged as if someone had typed it at a prompt.

    It ends after the TERMINATOR line, which `heredoc_spans` deliberately excludes: a
    gated literal there is the delimiter, not prose, but it is not a command either, and
    leaving it in makes `EOF` look like the name of something the chain runs.
    """
    spans: list[tuple[int, int]] = []
    for start, end, _, _ in heredoc_spans(command):
        eol = command.find("\n", end)
        spans.append((start - 1, len(command) if eol == -1 else eol))
    return spans


def _inert(command: str) -> str:
    """`command` with every span the shell will not execute blanked, length preserved.

    Newlines inside a heredoc go with it. `_blank` keeps them because a caller may want
    the line structure back; here they are the enemy, and were the whole of issue #233's
    misattribution — see `owned_spans`.
    """
    out = list(_QUOTED.sub(lambda m: " " * (m.end() - m.start()),
                           _blank(command, owned_spans(command))))
    for start, end in owned_spans(command):
        for i in range(max(0, start), min(len(out), end)):
            out[i] = " "
    return "".join(out)


def scannable(command: str) -> str:
    """The part of `command` that could actually *execute* something.

    Blanks quoted arguments so that merely naming a privileged action doesn't trip its
    gate — `git commit -m "document systemctl restart"` writes a commit message, and
    `jarvis learn add "…never run the release script…"` writes a note.

    A quoted payload IS code when something re-parses it (`sh -c`, `eval`, `xargs`), so
    those are scanned whole. Erring that way is deliberate: a spurious gate costs one
    review, a missed one ships unreviewed code.

    The invoker test is POSITIONAL — it runs on the blanked text, so only an invoker the
    shell would actually reach disarms blanking. Asking the raw string instead meant a
    payload that merely spelled one of those words turned blanking off for the whole
    command and gated itself (issue #203).

    Substitution is the exception, and it is tested BEFORE blanking rather than after:
    `$(…)`, backticks and `<(…)` run inside double quotes, so a span containing one is
    code however it is quoted. Prose that quotes a literal `$(` therefore gates — the
    loud failure, chosen over the silent one, and the same call `reads_only` makes.

    …AND WITH ONE KNOWN MISS, so read the paragraph above as conditional rather than a
    guarantee: a quoted span this blanks may still be EXECUTED, by something `_QUOTED`
    has no vocabulary for. `ssh host "…"` and `docker exec c "…"` run their payload, and
    neither is a shell invoker by this module's definition, so both the literal and any
    invoker naming it vanish in the same pass. The hole is older than the positional
    test — `ssh host "scripts/shipit.sh"` never gated — but the raw search used to catch
    the subset whose payload happened to spell one of the three keywords, and this makes
    the miss uniform. Issue #213, and it needs the one thing substitution did not: a
    notion of "wrapper that executes its quoted argument". Not a retreat to the raw
    search, which is issue #203.

    Heredoc bodies are deliberately NOT blanked here, and that is not the oversight it
    looks like. `cat <<EOF | bash` executes its body, so blanking it outright would open
    a bypass in the classifier for every gate at once. What the body needs is not a blunt
    exemption but a *learnable* one — see `Shape`, which records that a match landed in a
    body and lets a reviewed dismissal clear that shape for the chains that cannot run it.

    …and inside a body owned by an INTERPRETER, quotes are not blanked either, because
    the shell never sees them: `python - <<PY` hands the body to python, and a string
    literal in a python program is part of the program. Blanking them made
    `os.system("gh pr merge 223 --squash")` inside a heredoc match no recogniser at all
    (issue #233). The carve-out that clears such a body has to be `gate_paperwork` —
    which establishes that the literal is an ARGUMENT — rather than the accident of
    quoting, which establishes nothing.
    """
    if _SUBSTITUTION.search(command):
        return command
    programs = program_spans(command)
    # Replace rather than delete, so neighbouring tokens can't fuse into a false match.
    blanked = _QUOTED.sub(
        lambda m: m.group(0) if any(s <= m.start() and m.end() <= e for s, e in programs)
        else " ", command)
    return command if _SHELL_INVOKER.search(blanked) else blanked


def program_spans(command: str) -> list[tuple[int, int]]:
    """The heredoc bodies that are PROGRAMS rather than data: those owned by an executor.

    `git commit -F - <<EOF` is handed a commit message; `python - <<PY` is handed a
    program. Same syntax, and the difference is exactly the `_EXECUTORS` membership test.
    """
    heres = heredoc_spans(command)
    if not heres:
        return []
    segs = segments(command)
    spans: list[tuple[int, int]] = []
    for start, end, opener, _ in heres:
        owner = next((n for s, e, n in segs if s <= opener < e and n), "")
        if owner and owner.split()[0] in _EXECUTORS:
            spans.append((start, end))
    return spans


def _argv0(segment: str) -> str:
    """The name of the command a segment runs, `git commit` style, or `""`."""
    words = segment.strip().lstrip("({ ").split()
    while words and (_ASSIGNMENT.match(words[0]) or words[0].lstrip("\\") in _TRANSPARENT):
        words = words[1:]
    if not words:
        return ""
    name = words[0].lstrip("\\")
    if name in _SUBCOMMAND_TOOLS:
        rest = [w for w in words[1:] if not w.startswith("-")]
        if rest:
            name = f"{name} {rest[0]}"
    return name


def segments(command: str) -> list[tuple[int, int, str]]:
    """`(start, end, name)` for each command in the chain, offsets into `command`.

    Split on the *inert* form, so a separator inside a quoted argument or a heredoc body
    starts no new command: `grep "a\\|b" f | head` used to be torn in half at the
    alternation, and every line of a commit message would otherwise read as a command.
    """
    inert = _inert(command)
    bounds: list[tuple[int, int]] = []
    pos = 0
    for m in _SEPARATORS.finditer(inert):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(command)))
    return [(s, e, _argv0(inert[s:e])) for s, e in bounds]


def command_names(command: str) -> frozenset[str]:
    """Every command the chain runs. Blanked spans contribute nothing."""
    return frozenset(name for _, _, name in segments(command) if name)


def list_segments(command: str) -> list[tuple[int, int]]:
    """Spans of the independent commands in `command`, offsets into it.

    Splits on `||`, `&&`, `;`, `&` and newlines only — NOT on the pipe, which is what
    makes this different from `segments`. `a && b` is two commands that happen to be
    typed together; `a | b` is one command feeding another, and every judgement this
    module makes about a pipeline has to cover the whole of it.
    """
    inert = _inert(command)
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _LIST_SEPARATORS.finditer(inert):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(command)))
    return spans


def reads_only(command: str) -> bool:
    """True when every command in `command` can read but not execute.

    The exemption is all-or-nothing across the pipeline on purpose: a reader piping into
    a shell (`cat scripts/shipit.sh | bash`) is the obvious bypass, and one non-reading
    segment anywhere is enough to lose it. So the only commands this clears are ones
    that cannot run the thing they name — which is why it is safe to apply to every
    gate rather than just `release`.

    Applied to a whole chain this answers a whole-chain question, and across a list
    separator that is the wrong question — see `read_only_mentions`, which asks it of
    the one segment the gated literal is actually in.

    Structural, and therefore still in code rather than in the table: it is not a claim
    about any particular privileged action, it is a claim about what `cat` is.

    Unrecognised syntax fails the test rather than passing it: a name carrying a slash
    is not the `cat` on PATH but something in the tree that merely shares its name, an
    empty segment means the split found something this parser does not model, and an
    UNTERMINATED heredoc is a body that swallowed the rest of the string — the reader
    owning it now owns whatever came after the delimiter that never arrived, so it is
    not a reader (kn-67364b3a, and the guard that keeps `owned_spans` from paying for
    that ownership with a bypass).
    """
    # The asymmetry below is deliberate, not a half-finished edit, and `scannable` now
    # makes the same call: `_SUBSTITUTION` stays on the RAW command because `$(…)` runs
    # inside double quotes, so blanking first would hide the very thing it looks for. An
    # invoker does not — quote it and the shell passes it along as an argument — so that
    # test moved to the blanked text, where a reader's quoted argument reads as the thing
    # being read (issue #203).
    if _SUBSTITUTION.search(command):
        return False
    if any(not terminated for *_, terminated in heredoc_spans(command)):
        return False
    if _SHELL_INVOKER.search(_QUOTED.sub(" ", command)):
        return False
    # The INERT form, so a heredoc body stays with the command that owns it rather than
    # splitting into a line of prose that reads as a command nobody can name (#233).
    parts = [s.strip() for s in _SEPARATORS.split(_inert(command))]
    if not any(parts):
        return False
    for segment in parts:
        if not segment:
            continue
        words = segment.lstrip("({ ").split()
        while words and (_ASSIGNMENT.match(words[0])
                         or words[0].lstrip("\\") in _TRANSPARENT):
            words = words[1:]
        if not words:
            return False
        name = words[0].lstrip("\\")
        if name not in _READERS:
            return False
        # `sed -n '1,20p' f` reads; `sed -i s/a/b/ f` rewrites the file. `yq -i` too.
        if name in _INPLACE and any(w.startswith("-i") or w == "--in-place"
                                    for w in words[1:]):
            return False
    return True


def _hands_off(segment: str) -> bool:
    """Whether `segment` could leave the thing it read where a later command finds it.

    Commands either side of a list separator share no *stream*, but they do share the
    filesystem and the environment, and that is how a read reaches an executor without
    ever being piped to one: `cat scripts/shipit.sh > /tmp/s.sh && bash /tmp/s.sh` is a
    release written in two commands. Three ways to put it there — a redirection to a
    real file, a reader that writes one itself, and an assignment — and each test below
    is deliberately blunt, because being wrong here means a release that never asks.
    """
    inert = _inert(segment)
    if command_names(segment) & _MAY_WRITE:
        return True
    if any(_ASSIGNMENT.match(w.lstrip("({")) for w in inert.split()):
        return True
    # The operator is located in the inert form, so a `>` inside a quoted argument is not
    # a redirection — but the TARGET is read back out of the raw segment, because
    # `> "/tmp/s.sh"` is blanked there and an unreadable target must not read as none.
    for m in re.finditer(r">>?", inert):
        target = _REDIRECT_TARGET.match(segment[m.end():].lstrip())
        if target is None:
            return True
        name = target.group(0).strip("\"'")
        if not name.startswith("&") and name not in _DISCARD:
            return True
    return False


def read_only_mentions(command: str, pattern: str) -> bool:
    """True when every independent command that NAMES `pattern` can only read it.

    The segment-level form of `reads_only`, and the reason it exists: a `||` fallback
    guarding a read used to void the reader exemption for the read as well, so
    `cat …/shipit/SKILL.md || find ~ -name SKILL.md` gated as a release while either
    half alone did not (issue #194). `find` is genuinely an executor; it is just not in
    the same command as the literal.

    What stays chain-wide is everything the segment itself contains: a pipeline, a
    substitution or a heredoc body inside it is judged whole by `reads_only`, because
    there the reader's output really does reach the executor.

    …and so does everything, the moment the reading segment could WRITE what it read.
    A `_hands_off` reader puts the chain back under `reads_only`, because at that point
    the literal can travel by a route this parser does not follow. The two halves of
    that are the whole safety argument: `find` after a plain `cat` stays exempt, `bash`
    after a `cat >` does not.

    It has to be `reads_only` — the reader WHITELIST — and not "no known executor in the
    chain". A blacklist is one rename away from silence: `cat scripts/shipit.sh >
    /tmp/s.sh && chmod +x /tmp/s.sh && /tmp/s.sh` runs the release script without naming
    a single member of `_EXECUTORS`, and `chmod` is not something anyone will remember
    to add to it.

    Fails closed twice over. A pattern that matches no single segment — it straddled a
    separator, or only the whole string matched — clears nothing.
    """
    return bool(_mentions_only(command, pattern, paperwork=False))


def mentions_only(command: str, pattern: str) -> str:
    """WHY nothing that names `pattern` could run it, or `""` if something could.

    `read_only_mentions` widened by one segment kind: a segment that only hands the
    literal to `jarvis gate` paperwork runs it no more than `cat` does. Filing a case
    about an action is the OS's own sanctioned remedy, and it must survive being written
    in more than one command — issue #233, where a justification written to a file by
    `cat` and read back by the filing was held as the merge it was asking for.

    The reason is returned rather than a bool because it is recorded: a gate that says
    the wrong thing about why it fired is the other half of that issue.
    """
    return _mentions_only(command, pattern, paperwork=True)


#: How each kind of segment fails to run what it names. Ordered as the reason is read.
_CLEARANCES = ("can read it and nothing more", "hand it to `jarvis gate` paperwork, "
               "which files a claim about it and runs nothing")


def _mentions_only(command: str, pattern: str, *, paperwork: bool) -> str:
    """The loop both carve-outs share. One function so the safety argument is one place."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return ""
    handoff = False
    grounds: set[int] = set()
    for start, end in list_segments(command):
        segment = command[start:end]
        if not rx.search(scannable(segment)):
            continue
        if paperwork and files_a_claim(segment):
            grounds.add(1)
            continue
        if not reads_only(segment):
            return ""
        grounds.add(0)
        handoff = handoff or _hands_off(segment)
    if not grounds:
        return ""
    # A reader that WRITES what it read hands the literal on by a route no parser here
    # follows, so the whole chain has to be unable to run it — not just the segment the
    # literal is in. Paperwork counts there too, and for the same reason it counts above.
    if handoff and not (reads_only(command) or (paperwork and all(
            reads_only(command[s:e]) or files_a_claim(command[s:e])
            for s, e in list_segments(command)))):
        return ""
    if grounds == {0}:
        return f"only in commands that {_CLEARANCES[0]}"
    if grounds == {1}:
        return f"only in commands that {_CLEARANCES[1]}"
    return f"only in commands that {_CLEARANCES[0]}, or that {_CLEARANCES[1]}"


#: The verbs that RECORD or READ a claim about a command. Why these five and not the
#: deciding four: spec 2026-09-12 §9.
GATE_PAPERWORK_VERBS = frozenset({"request", "contest", "explain", "show", "list"})


def gate_paperwork(command: str) -> bool:
    """True when every command in the chain is the OS's own gate paperwork.

    All-or-nothing, and deliberately NOT guarded on `_SHELL_INVOKER` the way `reads_only`
    is. See docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §9.
    """
    if _SUBSTITUTION.search(command):
        return False
    spans = segments(command)
    if not spans:
        return False
    seen = False
    for start, end, name in spans:
        raw = command[start:end].strip()
        if not raw:
            continue
        if name != "jarvis gate":
            return False
        words = [w for w in raw.lstrip("({ ").split() if not w.startswith("-")]
        # `jarvis` `gate` `<verb>` — anything shorter is not addressed to these verbs.
        if len(words) < 3 or words[2] not in GATE_PAPERWORK_VERBS:
            return False
        seen = True
    return seen


def files_a_claim(segment: str) -> bool:
    """True when `segment` does nothing but RECORD or READ a claim about a command.

    Two routes to the same property, and the second is the one issue #233 adds: the
    paperwork may be typed at the shell, or passed to an interpreter as an argument. A
    worker whose justification runs to several paragraphs writes it with a heredoc and
    files it from python, and that is still paperwork — the OS's own advice, blocked.
    """
    return gate_paperwork(segment) or interpreter_paperwork(segment)


#: The `subprocess` entry points. A paperwork call is one of these, given an argv LIST
#: whose first three elements are literally `jarvis gate <verb>`.
_PY_RUNNERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

#: Everything else a filing script is allowed to call. Both lists are what the call CAN
#: do, the same membership test `_READERS` and `_EXECUTORS` are built on: `read_text`
#: reads a file, `system` runs a command, and no argument about intent enters into it.
_PY_INERT_FUNCS = frozenset({"open", "print", "str", "repr", "len", "int", "sorted",
                             "list", "tuple", "dict"})
_PY_INERT_METHODS = frozenset({"Path", "read", "read_text", "readlines", "strip",
                               "rstrip", "lstrip", "splitlines", "split", "join",
                               "format", "decode", "encode", "dumps", "loads"})

#: `shell=True` turns argv into a command line and `executable=` replaces the program, so
#: an allow-list is the only safe direction here.
_PY_RUN_KWARGS = frozenset({"capture_output", "text", "check", "cwd", "encoding",
                            "timeout", "stdout", "stderr", "stdin", "input"})

#: The whole grammar a filing script may be written in. Anything else — a loop, a
#: function, a `with`, a comprehension, an `import *` — is a program this cannot read,
#: and a program this cannot read is not one it may clear.
_PY_NODES = (ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.Assign, ast.AnnAssign,
             ast.Expr, ast.Call, ast.keyword, ast.Name, ast.Attribute, ast.Constant,
             ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Load,
             ast.Store, ast.BinOp, ast.Add, ast.JoinedStr, ast.FormattedValue)


def interpreter_paperwork(segment: str) -> bool:
    """True when `segment` is an interpreter whose whole program only files paperwork.

    The distinction this has to establish is ARGUMENT versus EXECUTION, and it is the
    half of issue #233 that could open a real hole: `subprocess.run(["jarvis", "gate",
    "request", …, "gh pr merge 223"])` passes the merge to a filing, while
    `os.system("gh pr merge 223")` runs it, and the two differ by nothing a regex can
    see. So this reads the program rather than the text — and answers False for every
    program it cannot read, which is most of them.

    Deliberately narrow. Python only, the program from the heredoc body and nowhere else
    (`-c` and a script path are both refused), one heredoc, no redirection, nothing else
    in the pipeline. Widening it is a change to a security boundary, not a convenience.
    """
    inert = _inert(segment).strip()
    words = inert.split()
    if not words or words[0] not in ("python", "python3"):
        return False
    # `-` is stdin, `<<PY` is the body it comes from. A script path, a `-c`, a `-m` or a
    # redirection all mean the program is not the one being read below.
    if any(w != "-" and not w.startswith("<<") for w in words[1:]):
        return False
    spans = heredoc_spans(segment)
    if len(spans) != 1 or not spans[0][3]:
        return False
    return python_files_only_paperwork(segment[spans[0][0]:spans[0][1]])


def python_files_only_paperwork(program: str) -> bool:
    """True when every call in `program` is inert, and at least one files gate paperwork.

    Reads the AST rather than the source, because the source is what the recogniser
    already failed to read. Fails closed on anything at all it does not model — an
    unparseable program, an unknown call, a grammar past `_PY_NODES`.
    """
    try:
        tree = ast.parse(program)
    except SyntaxError:
        return False
    filed = False
    for node in ast.walk(tree):
        if not isinstance(node, _PY_NODES):
            return False
        # Names only on the left of an `=`. `os.environ["PATH"] = "/tmp"` assigns no
        # command and calls nothing, and it decides which `jarvis` the filing below it
        # runs — the one thing a program this narrow could still do to the world.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                return False
        if isinstance(node, ast.Call):
            verdict = _py_call(node)
            if verdict is None:
                return False
            filed = filed or verdict
    return filed


def _py_call(node: ast.Call) -> bool | None:
    """`True` a filing, `False` inert, `None` anything this cannot place — which gates."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _PY_RUNNERS:
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            return None
        if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
            return None                       # a string first argument is a command line
        argv = [e.value for e in node.args[0].elts[:3]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if argv[:2] != ["jarvis", "gate"] or argv[2:3] == [] \
                or argv[2] not in GATE_PAPERWORK_VERBS:
            return None
        if any(kw.arg is None or kw.arg not in _PY_RUN_KWARGS for kw in node.keywords):
            return None                       # `shell=True`, `executable=`, `**kwargs`
        return True
    if isinstance(func, ast.Name) and func.id in _PY_INERT_FUNCS:
        # `open(p)` reads; `open(p, "w")` is a file the next command in the chain runs.
        if func.id == "open" and (len(node.args) > 1 or node.keywords):
            return None
        return False
    if isinstance(func, ast.Attribute) and func.attr in _PY_INERT_METHODS:
        return False
    return None


# -- the shape of a match ------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """Where a gated literal turned up in a command, and what owns that position.

    This is the whole vocabulary of a structural exemption, and it is small on purpose.
    A learned rule may say "the release script named in the heredoc body of a `git
    commit` is prose" — it has no way to say anything about a literal the shell will run,
    because `position == CODE` is not exemptible and nothing can make it so.
    """

    position: str            # code | heredoc | quoted
    owner: str               # the command that owns that position, e.g. `git commit`
    names: frozenset[str]    # every command name in the match's own list segment
    handoff: bool = False    # the owner could leave the literal where a later command
                             # finds it — `_hands_off`

    @property
    def exemptible(self) -> bool:
        """Whether a shape like this may ever be cleared by a learned rule.

        Four conditions, each closing a different door:
        - the literal is not in executable position;
        - something owns the position (an unparseable command is not a known-safe one);
        - nothing that could execute the span the literal sits in shares a segment with
          it. This is the condition that keeps `cat <<EOF | bash` and `eval "…"` gated no
          matter what was dismissed before, and it is checked against the segment rather
          than the owner because the executor is usually downstream of it — but not
          against the whole chain, where an executor past a `||` never sees the literal
          at all (issue #194);
        - the owner does not WRITE the literal somewhere. A signature is `{position,
          owner}` and nothing else, so a rule learned from `cat > /tmp/s.sh <<EOF` would
          read as "a heredoc body owned by `cat` is prose" and clear the same body in a
          chain that then runs the file. `_hands_off` is already why such a chain gates;
          without this it would gate once and be dismissed into a standing exemption.
        """
        return (self.position in EXEMPTIBLE_POSITIONS
                and bool(self.owner)
                and not self.handoff
                and not (self.names & _EXECUTORS))

    def signature(self) -> str:
        return json.dumps({"position": self.position, "owner": self.owner},
                          sort_keys=True)

    @property
    def blockers(self) -> tuple[str, ...]:
        """What in the literal's own segment could run it."""
        return tuple(sorted(self.names & _EXECUTORS))

    def unlearnable_reason(self) -> str:
        """Why no dismissal of this shape could become a rule, or `""` if one could."""
        if self.position not in EXEMPTIBLE_POSITIONS:
            return "the literal is where the shell would run it"
        if not self.owner:
            return "nothing recognisable owns the position the literal sits in"
        if self.blockers:
            return f"its own command executes via {', '.join(self.blockers)}"
        if self.handoff:
            return f"`{self.owner}` writes what it was given, so a later command may run it"
        return ""

    def describe(self) -> str:
        where = {CODE: "in executable position", HEREDOC: "in a heredoc body",
                 QUOTED: "inside a quoted argument"}.get(self.position, self.position)
        note = (f"; that command executes via {', '.join(self.blockers)}"
                if self.blockers else
                "; that command writes it out, where a later one may run it"
                if self.handoff else "")
        return f"{where}, owned by `{self.owner or '?'}`{note}"


def shape_of(command: str, pattern: str) -> Shape | None:
    """Where `pattern` matches inside `command`, or None if it does not occur there.

    Ambiguity resolves toward the privileged reading, which is why a literal appearing
    BOTH in a heredoc body and in executable position reports `code`: the settled
    convention is that if you cannot tell whether the command runs the thing or only
    mentions it, it runs it.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    heres = heredoc_spans(command)
    quotes = [m.span() for m in
              _QUOTED.finditer(_blank(command, [(s, e) for s, e, _, _ in heres]))]
    segs = segments(command)
    lists = list_segments(command)
    all_names = frozenset(n for _, _, n in segs if n)

    def owner_at(offset: int) -> str:
        for s, e, name in segs:
            if s <= offset < e and name:
                return name
        return ""

    def names_at(offset: int) -> frozenset[str]:
        """What shares a command with the match. Falls back to the whole chain when the
        offset lands nowhere, so an unmodelled string cannot lose its executors."""
        for s, e in lists:
            if s <= offset < e:
                return command_names(command[s:e])
        return all_names

    def handoff_at(offset: int) -> bool:
        """Whether the command holding the match writes what it was handed."""
        for s, e in lists:
            if s <= offset < e:
                return _hands_off(command[s:e])
        return True

    best: Shape | None = None
    for m in rx.finditer(command):
        start, end = m.span()
        for s, e, opener, terminated in heres:
            if s <= start and end <= e:
                # An unterminated body runs to the end of the string and swallows
                # whatever follows it, so nothing inside one can be called prose.
                # `EOF && ./scripts/shipit.sh` is that case: the delimiter line is not
                # a delimiter line, and the release below it was landing in the "body".
                if not terminated:
                    return Shape(CODE, owner_at(start), names_at(start),
                                 handoff_at(start))
                shape = Shape(HEREDOC, owner_at(opener), names_at(opener),
                              handoff_at(opener))
                break
        else:
            for s, e in quotes:
                if s <= start and end <= e:
                    shape = Shape(QUOTED, owner_at(start), names_at(start),
                                  handoff_at(start))
                    break
            else:
                # Executable position wins outright, and immediately.
                return Shape(CODE, owner_at(start), names_at(start),
                             handoff_at(start))
        # Among several non-code matches, report the one that can LEAST be cleared, not
        # the one that happened to come first. Same convention as the `code` return
        # above: ambiguity resolves toward the privileged reading. Scoping `names` to a
        # segment made first-wins a bypass — a `git commit` heredoc followed by
        # `cat <<EOF | bash` put the executors in the other segment, so the leading
        # match looked like prose and cleared the trailing one (wo-551f5e8c).
        if best is None or (best.exemptible and not shape.exemptible):
            best = shape
    return best


# -- a rule ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One row of the rule base."""

    id: str
    role: str
    test: str
    pattern: str
    kind: str = ""              # "" on an exemption means "every gate"
    summary: str = ""
    source: str = "builtin"
    project: str = ""
    wo_id: str = ""
    approval_id: int | None = None
    reason: str = ""
    hits: int = 0
    ts: float = 0.0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Rule:
        return cls(
            id=row["id"], role=row["role"], test=row["test"], pattern=row["pattern"],
            kind=row.get("kind") or "", summary=row.get("summary") or "",
            source=row.get("source") or "builtin", project=row.get("project") or "",
            wo_id=row.get("wo_id") or "", approval_id=row.get("approval_id"),
            reason=row.get("reason") or "", hits=int(row.get("hits") or 0),
            ts=float(row.get("ts") or 0.0),
        )

    @property
    def payload(self) -> dict[str, Any]:
        """The parsed `signature` body. `{}` for any other test."""
        if self.test != SIGNATURE:
            return {}
        try:
            data = json.loads(self.pattern)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def render(self) -> str:
        """The pattern as a human reads it."""
        if self.test != SIGNATURE:
            return self.pattern
        p = self.payload
        return (f"a `{p.get('kind') or self.kind}` literal "
                f"{'in a heredoc body' if p.get('position') == HEREDOC else 'inside a quoted argument'} "
                f"owned by `{p.get('owner')}`, in a command that executes nothing")

    def clears(self, command: str, kind: str, pattern: str) -> bool:
        """Whether this exemption clears a match of `pattern` in `command`.

        The two restrictions on the REGEX arm are the floor under a reviewer-authored
        pattern, and they hold for a rule already stored — which is the case that
        mattered. See docs/superpowers/specs/2026-09-12-an-exemption-may-not-clear-what-
        it-does-not-describe.md §3.
        """
        if self.role != EXEMPT:
            return False
        if self.kind and self.kind != kind:
            return False
        if self.test == REGEX:
            # One normalisation, then both tests read the same string — a trailing
            # newline must not decide whether an exemption applies.
            subject = command.strip()
            # A regex exemption speaks about one line. It is learned from a single-line
            # command (`propose_exemption`) and cannot say which line of a script it
            # describes, so it clears none of them.
            if "\n" in subject:
                return False
            try:
                # `fullmatch`, not `search`: an exemption argues that a command is
                # harmless, and a claim about part of one says nothing about the rest.
                return bool(re.fullmatch(self.pattern, subject, re.IGNORECASE))
            except re.error:
                return False
        if self.test == SIGNATURE:
            want = self.payload
            shape = shape_of(command, pattern)
            if shape is None or not shape.exemptible:
                return False
            return (shape.position == want.get("position")
                    and shape.owner == want.get("owner"))
        return False


def seed_id(role: str, kind: str, test: str, pattern: str) -> str:
    """A stable id for a seeded rule.

    Derived from the content so that re-seeding is idempotent across upgrades AND cannot
    resurrect a rule the user retracted: the row already exists, so the insert is ignored
    rather than replayed. A random id would quietly restore a retired recogniser on every
    release, which is the sort of bug nobody finds until it matters.
    """
    digest = hashlib.sha1(f"{role}|{kind}|{test}|{pattern}".encode()).hexdigest()
    return f"gr-{digest[:10]}"


# -- what the table is seeded with ----------------------------------------------------

# The recognisers. Deliberately broad: a false positive costs one Neo review, a false
# negative lets a worker ship unreviewed. Anchored on the verbs that actually publish
# something, so ordinary work (pushing a feature branch, force-pushing one's own PR
# branch, running tests) never trips a gate.
#
# These are SEEDS. Nothing reads them to classify a command — they are written into
# `gate_rules` on first use and read back from there, which is what lets a dismissal
# change the answer without a release.
SEED_MATCHES: tuple[tuple[str, str], ...] = (
    ("pr_merge", r"\bgh\s+pr\s+merge\b"),
    ("pr_merge", r"\bgh\s+api\b[^\n]*\bpulls/\d+/merge\b"),
    ("release", r"shipit"),                      # the OS's own release script
    ("release", r"\bgh\s+release\s+create\b"),
    ("release", r"\bnpm\s+publish\b"),
    ("release", r"\b(twine|uv)\s+publish\b"),
    ("release", r"\bgit\s+push\b[^\n]*--(tags|follow-tags)\b"),
    ("service_restart", r"\bsystemctl\b[^\n]*\b(restart|stop|start|disable|enable)\b"),
    ("push_protected", r"\bgit\s+push\b[^\n]*\b(origin\s+)?(main|master)\b"),
    ("push_protected", r"\bgit\s+push\b[^\n]*\bHEAD:(refs/heads/)?(main|master)\b"),
    ("config_write", r"\bjarvis\s+config\s+(set|unset|restore|adopt)\b"),
)

# Commands that must gate, forever, whatever anyone learns. Every proposed exemption is
# run against the whole set before it is allowed into the table; a proposal that would
# clear any of these is rejected and the rejection is recorded on the work order.
#
# The last three of the release group are not examples of anything a worker would type.
# They are there because they are the shapes a *learned* rule would most plausibly clear
# by accident: a heredoc body piped into a shell, a quoted payload handed to `eval`, and
# a heredoc body handed to an interpreter. All three are the exact spans an exemption
# talks about, in the one arrangement where they are code after all.
SEED_CANARIES: tuple[tuple[str, str], ...] = (
    ("pr_merge", "gh pr merge 31 --squash --delete-branch"),
    ("pr_merge", "gh pr merge --auto"),
    ("pr_merge", "gh api --method PUT repos/o/r/pulls/31/merge"),
    ("release", "./scripts/shipit.sh"),
    ("release", "bash scripts/shipit.sh --dry-run"),
    ("release", "gh release create jarvis-1.2.3"),
    ("release", "npm publish"),
    ("release", "uv publish"),
    ("release", "git push --follow-tags origin release/jarvis-1.2.3"),
    ("release", "cat <<'EOF' | bash\nscripts/shipit.sh\nEOF"),
    ("release", 'eval "bash scripts/shipit.sh"'),
    # Added with the issue #203 fix, to pin what that fix must NOT turn into a miss.
    ("release", "python3 - <<'PY'\nscripts/shipit.sh\nPY"),
    # The pipe the reader exemption must never be loosened across, and the list
    # separator it must be — `reader || executor` is two commands (issue #194), and
    # these two lines are what stops the second fix reaching the first.
    ("release", "cat scripts/shipit.sh | bash"),
    ("release", "ls -la .claude/skills/shipit/ && ./scripts/shipit.sh"),
    # …and the one that is neither: a reader whose output is WRITTEN somewhere the next
    # command runs it. Nothing pipes here and no executor names the script.
    ("release", "cat scripts/shipit.sh > /tmp/s.sh && bash /tmp/s.sh"),
    # The same, run without naming any member of `_EXECUTORS` at all. Pins that the
    # handoff falls back to the reader whitelist rather than to a list of bad names.
    ("release", "cat scripts/shipit.sh > /tmp/s.sh && chmod +x /tmp/s.sh && /tmp/s.sh"),
    ("service_restart", "sudo systemctl restart jarvis-daemon"),
    ("service_restart", "systemctl --user stop jarvisd"),
    ("push_protected", "git push origin main"),
    ("push_protected", "git push -f origin HEAD:refs/heads/master"),
    # The recursion this cuts: the console can turn gates off, so the command that turns
    # them off must never become exemptible. See the spec's §7.
    ("config_write", "jarvis config set proj_a worker.permission_mode bypassPermissions "
                     "--reason \"faster\""),
    ("config_write", "jarvis config unset os.validation.enabled --reason \"noisy\""),
    ("config_write", "jarvis config restore cfg-0123456789abcdef --reason \"revert\""),
    ("config_write", "jarvis config adopt --reason \"hand edit\""),
    # One per kind: a reader, a newline, and the canonical gated command. Until
    # wo-551f5e8c every canary was a single line, so a green report was evidence about
    # single-line commands only — the spec's §5.
    ("release", "echo hi\n./scripts/shipit.sh"),
    ("pr_merge", "cat README.md\ngh pr merge 31 --squash"),
    ("service_restart", "cat README.md\nsudo systemctl restart jarvis-daemon"),
    ("push_protected", "ls -la\ngit push origin main"),
    ("config_write", "head -5 README.md\njarvis config set p worker.model haiku "
                     "--reason \"cheaper\""),
)


def seed_rows() -> list[dict[str, Any]]:
    """Every seeded rule, as store rows. Ids are content-derived — see `seed_id`."""
    rows: list[dict[str, Any]] = []
    for kind, pattern in SEED_MATCHES:
        rows.append({
            "id": seed_id(MATCH, kind, REGEX, pattern), "role": MATCH, "kind": kind,
            "test": REGEX, "pattern": pattern, "source": "builtin",
            "summary": f"recognises an attempt to {dict((k.name, k.summary) for k in KINDS)[kind]}",
        })
    for kind, command in SEED_CANARIES:
        rows.append({
            "id": seed_id(CANARY, kind, COMMAND, command), "role": CANARY, "kind": kind,
            "test": COMMAND, "pattern": command, "source": "builtin",
            "summary": "must always gate — no learned rule may clear it",
        })
    return rows


# 2: the `config_write` kind, its recogniser and its canaries (the config console).
# 3: the reader-exemption canaries — the pipe, the list separator, and the reader whose
#    output is written somewhere the next command runs it (issue #194).
# 4: the `python3 - <<PY` release canary (issue #203). A separate number because 3 has
#    already been written to live databases, and `_seed_gate_rules` returns early on a
#    matching key — reusing it would leave that canary out of every os.db that has one.
# 5: the multi-line canaries (wo-551f5e8c). Same reasoning as 4 — 3 and 4 are already
#    written, so these need a number of their own or no live os.db ever gets them.
SEED_VERSION = "5"


# -- the live rule base ---------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """A recogniser fired, and nothing cleared it."""

    kind: str
    pattern: str
    rule_id: str


@dataclass(frozen=True)
class Decision:
    """The classifier's full reasoning, for the gate and for anyone debugging one."""

    match: Match | None
    cleared: tuple[tuple[str, str, str], ...] = ()  # (rule_id, kind, pattern)
    trace: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleSet:
    """The rules in force. Immutable; `with_rule` returns a trial copy."""

    rules: tuple[Rule, ...] = ()

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> RuleSet:
        return cls(rules=tuple(Rule.from_row(r) for r in rows))

    @classmethod
    def from_seeds(cls) -> RuleSet:
        """The seeds, as a RuleSet.

        The fallback for a database that cannot be read — and note the direction it errs
        in. It restores every recogniser and NO exemption, so an unreadable `os.db` makes
        the gate over-eager rather than absent. A worker gets a spurious review; nothing
        ships unreviewed.
        """
        return cls.from_rows(seed_rows())

    @classmethod
    def load(cls, central: CentralStore | None = None) -> RuleSet:
        """The rules as the database holds them, seeding it first if it is new."""
        from .central_store import CentralStore as _CS

        store = central or _CS()
        try:
            return cls.from_rows(store.gate_rules())
        finally:
            if central is None:
                store.close()

    # -- views ------------------------------------------------------------------

    def of_role(self, role: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.role == role)

    def matchers(self) -> tuple[Rule, ...]:
        """Recognisers, in gate order — so that a command tripping two gates is reported
        as the same one it was before the rules moved into a table."""
        return tuple(sorted(self.of_role(MATCH),
                            key=lambda r: (KIND_ORDER.get(r.kind, 99), r.ts, r.id)))

    def exemptions(self) -> tuple[Rule, ...]:
        return self.of_role(EXEMPT)

    def canaries(self) -> tuple[Rule, ...]:
        return self.of_role(CANARY)

    def with_rule(self, rule: Rule) -> RuleSet:
        return RuleSet(rules=(*self.rules, rule))

    # -- classification ---------------------------------------------------------

    def decide(self, command: str, enabled: Iterable[str],
               extra_patterns: dict[str, tuple[str, ...]] | None = None) -> Decision:
        """Which gate `command` trips, why, and what cleared it if nothing did.

        Matches the whole command rather than parsed segments: a gated action hidden in a
        pipeline, a subshell or behind `&&` is the same action, and a classifier that only
        understands well-formed simple commands is a classifier with a bypass.
        """
        live = set(enabled)
        if not command or not live:
            return Decision(None, trace=("no gate is enabled",))
        if reads_only(command):
            return Decision(None, trace=("every command in the chain can only read",))
        # Before the table, because no entry in the table can state it — §9.
        if gate_paperwork(command):
            return Decision(None, trace=(
                "every command in the chain is `jarvis gate` paperwork: it files or "
                "reads a claim ABOUT a command and runs nothing",))
        haystack = scannable(command)
        trace: list[str] = []
        cleared: list[tuple[str, str, str]] = []

        candidates: list[tuple[str, str, str]] = [
            (r.kind, r.pattern, r.id) for r in self.matchers() if r.kind in live
        ]
        # Catalog-supplied patterns stay project config rather than learned state: they
        # describe THIS repo's deploy script, and belong with the project that has one.
        for kind, pats in sorted((extra_patterns or {}).items()):
            if kind in live:
                candidates += [(kind, p, "catalog") for p in pats]

        for kind, pattern, rule_id in candidates:
            try:
                if not re.search(pattern, haystack, re.IGNORECASE):
                    continue
            except re.error:
                trace.append(f"rule {rule_id} does not compile: {pattern!r}")
                continue
            why = mentions_only(command, pattern)
            if why:
                trace.append(f"{kind} matched {pattern!r} but {why}")
                continue
            exemption = self.clearance(command, kind, pattern)
            if exemption is not None:
                cleared.append((exemption.id, kind, pattern))
                trace.append(
                    f"{kind} matched {pattern!r} but rule {exemption.id} clears it: "
                    f"{exemption.render()}"
                )
                continue
            trace.append(f"{kind} matched {pattern!r} (rule {rule_id})")
            return Decision(Match(kind=kind, pattern=pattern, rule_id=rule_id),
                            cleared=tuple(cleared), trace=tuple(trace))
        if not trace:
            trace.append("no recogniser matched")
        return Decision(None, cleared=tuple(cleared), trace=tuple(trace))

    def clearance(self, command: str, kind: str, pattern: str) -> Rule | None:
        for rule in self.exemptions():
            if rule.clears(command, kind, pattern):
                return rule
        return None

    # -- the safety net ---------------------------------------------------------

    def check_canaries(self, candidate: Rule | None = None) -> list[dict[str, str]]:
        """Canaries that would STOP gating. Empty is the healthy answer.

        With `candidate`, the question is "may this rule exist?" — the trial ruleset is
        this one plus the candidate. Without, it is "is the rule base still sound?", which
        is what `INV-GATE-CANARY` and `jarvis doctor` ask, and it catches the other way in:
        a retracted or edited recogniser that leaves a real privileged action unrecognised.
        """
        trial = self.with_rule(candidate) if candidate is not None else self
        failures: list[dict[str, str]] = []
        for canary in trial.canaries():
            decision = trial.decide(canary.pattern, KIND_NAMES)
            if decision.match is None:
                failures.append({"command": canary.pattern, "kind": canary.kind,
                                 "why": "no gate fires on it any more"})
            elif decision.match.kind != canary.kind:
                failures.append({"command": canary.pattern, "kind": canary.kind,
                                 "why": f"now gates as {decision.match.kind}"})
        return failures


# -- learning from a dismissal --------------------------------------------------------

# A reviewer-authored pattern has to survive this before it is allowed to clear anything.
# None of these are about the regex being *good*; they are about it being a statement
# rather than a blank cheque.
_MAX_PATTERN = 400
_LITERAL_ANCHOR = re.compile(r"[A-Za-z0-9_]{3,}")


def _spans_a_newline(rx: re.Pattern[str], command: str) -> bool:
    """Whether `rx` would still clear `command` with a gated command appended below it.

    Probed rather than parsed, and the spec's §4 says why. The probes are the
    single-line `SEED_CANARIES` — the commands that must gate whatever anyone learns.

    Empty that set and `any()` returns False for every pattern, which is this module's
    own §5 failure a second time: a floor that reports sound because it tested nothing.
    So it raises instead.
    """
    probes = [gated for _, gated in SEED_CANARIES if "\n" not in gated]
    if not probes:
        raise AssertionError(
            "no single-line canary to probe with: the newline floor would pass "
            "everything. SEED_CANARIES must keep at least one single-line command.")
    return any(rx.search(f"{command}\n{gated}") for gated in probes)


def validate_pattern(pattern: str, command: str) -> str:
    """Why `pattern` may not be used as an exemption, or `""` if it may.

    The literal-anchor test is the one doing real work. A reviewer that answers with
    `.*`, `.+` or `git.*` is not describing a family of false positives, it is switching
    the gate off; requiring three consecutive literal characters costs a genuine rule
    nothing and makes the blanket cases unrepresentable.

    The whole-command and newline tests are the same two claims `Rule.clears` enforces
    at use time. Both live in both places on purpose — the spec's §4.
    """
    pattern = pattern.strip()
    command = command.strip()  # one normalisation; `Rule.clears` uses the same subject
    if not pattern:
        return "empty"
    if len(pattern) > _MAX_PATTERN:
        return f"longer than {_MAX_PATTERN} characters"
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"does not compile: {e}"
    if not _LITERAL_ANCHOR.search(pattern):
        return ("no literal anchor — a pattern this general would clear commands nobody "
                "has reviewed")
    if not rx.search(command):
        return "does not match the command it was written for"
    if not rx.fullmatch(command):
        return ("does not cover the whole command it was written for — an exemption that "
                "describes a prefix clears whatever is chained after it")
    if _spans_a_newline(rx, command):
        return ("matches across a newline — it would clear every line below the command "
                "it describes, whatever those lines run")
    return ""


@dataclass
class Proposal:
    """What the OS decided to learn from one dismissal, and what it refused to learn."""

    rule: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def learned(self) -> bool:
        return self.rule is not None


def propose_exemption(ruleset: RuleSet, *, command: str, kind: str, pattern: str,
                      reason: str = "", exempt_pattern: str = "",
                      approval_id: int | None = None, wo_id: str = "",
                      project: str = "", rule_id: str = "") -> Proposal:
    """Turn a dismissal into a rule, or explain why it could not become one.

    Two candidates are tried in order, and the order is the design: the reviewer's own
    generalisation first, because it can describe a family the OS cannot infer from one
    example; the structural signature second, as the floor that needs no reviewer at all.
    Whichever is used, it must leave every canary gating.

    Returning nothing is a perfectly good outcome — a dismissal whose command had the
    release literal in executable position teaches nothing that can be safely generalised,
    and saying so in the notes is more useful than inventing a rule.
    """
    proposal = Proposal()
    shape = shape_of(command, pattern)

    def already_have(rule: Rule) -> bool:
        """Whether an identical rule is already in force.

        Normally unreachable — a shape already exempted never trips a gate, so it never
        reaches a reviewer — but two dismissals decided in the same tick can both look
        novel. Cheaper to check than to explain a rule base with the same row in it four
        times.
        """
        return any(r.test == rule.test and r.pattern == rule.pattern
                   and r.kind == rule.kind for r in ruleset.exemptions())

    if exempt_pattern.strip():
        # A multi-line command generalises into a regex only by deciding which of its
        # lines mattered, which is the decision that opened the release gate. The
        # structural signature below reads the shape instead, and is the path a heredoc
        # was always meant to take.
        why = ("the command spans more than one line, and no regex may be learned from "
               "one" if "\n" in command.strip()
               else validate_pattern(exempt_pattern, command))
        if why:
            proposal.notes.append(
                f"the reviewer's proposed pattern {exempt_pattern!r} was refused: {why}")
        else:
            candidate = Rule(
                id=rule_id or "gr-candidate", role=EXEMPT, test=REGEX,
                pattern=exempt_pattern.strip(), kind=kind, source="neo",
                project=project, wo_id=wo_id, approval_id=approval_id, reason=reason,
                summary="proposed by the reviewer that dismissed the false positive",
            )
            failures = ruleset.check_canaries(candidate)
            if failures:
                proposal.notes.append(
                    "the reviewer's proposed pattern was refused: it would stop gating "
                    + ", ".join(f"`{f['command'].splitlines()[0]}`" for f in failures)
                )
            elif already_have(candidate):
                proposal.notes.append("an identical rule is already in force")
                return proposal
            else:
                proposal.rule = _as_row(candidate)
                return proposal

    if shape is None:
        proposal.notes.append(
            "nothing was learned: the recogniser's pattern does not occur in the command "
            "as written, so its shape could not be determined")
        return proposal
    if not shape.exemptible:
        proposal.notes.append(
            f"nothing was learned: the literal is {shape.describe()}, and a shape like "
            f"that can never be cleared by a learned rule")
        return proposal

    candidate = Rule(
        id=rule_id or "gr-candidate", role=EXEMPT, test=SIGNATURE, kind=kind,
        pattern=json.dumps({"position": shape.position, "owner": shape.owner,
                            "kind": kind}, sort_keys=True),
        source="neo", project=project, wo_id=wo_id, approval_id=approval_id,
        reason=reason, summary="structural shape of a dismissed false positive",
    )
    failures = ruleset.check_canaries(candidate)
    if failures:
        proposal.notes.append(
            "nothing was learned: the structural shape would stop gating "
            + ", ".join(f"`{f['command'].splitlines()[0]}`" for f in failures))
        return proposal
    if already_have(candidate):
        proposal.notes.append("an identical rule is already in force")
        return proposal
    proposal.rule = _as_row(candidate)
    return proposal


def _as_row(rule: Rule) -> dict[str, Any]:
    return {
        "role": rule.role, "kind": rule.kind, "test": rule.test, "pattern": rule.pattern,
        "summary": rule.summary, "source": rule.source, "project": rule.project,
        "wo_id": rule.wo_id, "approval_id": rule.approval_id, "reason": rule.reason,
    }
