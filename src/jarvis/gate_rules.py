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
signature apply to any chain containing an executor (`bash`, `eval`, `xargs`, `python`):
`git commit <<EOF … shipit … EOF` is a commit message, `cat <<EOF | bash … shipit … EOF`
is a release, and the difference between them is exactly the executor in the chain.
"""

from __future__ import annotations

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
        conflict_markers=("gh pr merge", "pulls/"),
    ),
    GateKind(
        name="release",
        summary="cut a release and deploy it (this reaches the live production fleet)",
        conflict_markers=("shipit", "gh release", "npm publish", "twine publish",
                          "uv publish", "--tags", "--follow-tags"),
    ),
    GateKind(
        name="service_restart",
        summary="restart or stop a system service (this interrupts the running fleet)",
        conflict_markers=("systemctl",),
    ),
    GateKind(
        name="push_protected",
        summary="push directly to a protected branch, bypassing review",
        conflict_markers=("git push",),
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

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def heredoc_spans(command: str) -> list[tuple[int, int, int]]:
    """`(body_start, body_end, opener_start)` for every heredoc in `command`.

    The body of a heredoc is the one span of a command that is neither quoted nor code:
    the shell reads it verbatim and hands it to the command as data. `_QUOTED` cannot see
    it — nothing about it is quoted — which is why `git commit -F -` with a message
    mentioning the release script was gated as a release (issue #104).

    Offsets are into the original string, so a caller can ask where a match landed.
    """
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for m in _HEREDOC_OPEN.finditer(command):
        # An opener inside an earlier heredoc's body is text, not an opener.
        if any(s <= m.start() < e for s, e, _ in spans):
            continue
        delim = m.group("sq") or m.group("dq") or m.group("bare")
        # The body starts on the line after the opener — or, when a second heredoc opens
        # on the same line, after the first one's terminator.
        nl = command.find("\n", max(m.end(), cursor))
        if nl == -1:
            continue  # an opener with no body: nothing was ever read
        start = nl + 1
        end = len(command)
        pos = start
        while pos <= len(command):
            eol = command.find("\n", pos)
            line = command[pos:eol if eol != -1 else len(command)]
            if line.strip() == delim:
                end = pos
                break
            if eol == -1:
                break
            pos = eol + 1
        spans.append((start, end, m.start()))
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


def _inert(command: str) -> str:
    """`command` with every span the shell will not execute blanked, length preserved.

    The heredoc's TERMINATOR line goes too, which `heredoc_spans` deliberately does not
    include: it is not part of the body (a gated literal there is the delimiter, not
    prose), but it is not a command either, and leaving it in makes `EOF` look like the
    name of something the chain runs.
    """
    spans: list[tuple[int, int]] = []
    for start, end, _ in heredoc_spans(command):
        eol = command.find("\n", end)
        spans.append((start, len(command) if eol == -1 else eol))
    return _QUOTED.sub(lambda m: " " * (m.end() - m.start()), _blank(command, spans))


def scannable(command: str) -> str:
    """The part of `command` that could actually *execute* something.

    Blanks quoted arguments so that merely naming a privileged action doesn't trip its
    gate — `git commit -m "document systemctl restart"` writes a commit message, and
    `jarvis learn add "…never run the release script…"` writes a note.

    A quoted payload IS code when something re-parses it (`sh -c`, `eval`, `xargs`), so
    those are scanned whole. Erring that way is deliberate: a spurious gate costs one
    review, a missed one ships unreviewed code.

    Heredoc bodies are deliberately NOT blanked here, and that is not the oversight it
    looks like. `cat <<EOF | bash` executes its body, so blanking it outright would open
    a bypass in the classifier for every gate at once. What the body needs is not a blunt
    exemption but a *learnable* one — see `Shape`, which records that a match landed in a
    body and lets a reviewed dismissal clear that shape for the chains that cannot run it.
    """
    if _SHELL_INVOKER.search(command):
        return command
    # Replace rather than delete, so neighbouring tokens can't fuse into a false match.
    return _QUOTED.sub(" ", command)


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


def reads_only(command: str) -> bool:
    """True when every command in `command` can read but not execute.

    The exemption is all-or-nothing across the pipeline on purpose: a reader piping into
    a shell (`cat scripts/shipit.sh | bash`) is the obvious bypass, and one non-reading
    segment anywhere is enough to lose it. So the only commands this clears are ones
    that cannot run the thing they name — which is why it is safe to apply to every
    gate rather than just `release`.

    Structural, and therefore still in code rather than in the table: it is not a claim
    about any particular privileged action, it is a claim about what `cat` is.

    Unrecognised syntax fails the test rather than passing it: a name carrying a slash
    is not the `cat` on PATH but something in the tree that merely shares its name, and
    an empty segment means the split found something this parser does not model.
    """
    if _SUBSTITUTION.search(command) or _SHELL_INVOKER.search(command):
        return False
    parts = [s.strip() for s in _SEPARATORS.split(_QUOTED.sub(" ", command))]
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
        # `sed -n '1,20p' f` reads; `sed -i s/a/b/ f` rewrites the file.
        if name == "sed" and any(w.startswith("-i") or w == "--in-place"
                                 for w in words[1:]):
            return False
    return True


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
    names: frozenset[str]    # every command name in the chain

    @property
    def exemptible(self) -> bool:
        """Whether a shape like this may ever be cleared by a learned rule.

        Three conditions, each closing a different door:
        - the literal is not in executable position;
        - something owns the position (an unparseable command is not a known-safe one);
        - nothing in the chain can execute the span the literal sits in. This is the
          condition that keeps `cat <<EOF | bash` and `eval "…"` gated no matter what was
          dismissed before, and it is checked against the chain rather than the owner
          because the executor is usually downstream of it.
        """
        return (self.position in EXEMPTIBLE_POSITIONS
                and bool(self.owner)
                and not (self.names & _EXECUTORS))

    def signature(self) -> str:
        return json.dumps({"position": self.position, "owner": self.owner},
                          sort_keys=True)

    def describe(self) -> str:
        where = {CODE: "in executable position", HEREDOC: "in a heredoc body",
                 QUOTED: "inside a quoted argument"}.get(self.position, self.position)
        blockers = sorted(self.names & _EXECUTORS)
        note = f"; chain executes via {', '.join(blockers)}" if blockers else ""
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
              _QUOTED.finditer(_blank(command, [(s, e) for s, e, _ in heres]))]
    segs = segments(command)
    names = frozenset(n for _, _, n in segs if n)

    def owner_at(offset: int) -> str:
        for s, e, name in segs:
            if s <= offset < e and name:
                return name
        return ""

    best: Shape | None = None
    for m in rx.finditer(command):
        start, end = m.span()
        for s, e, opener in heres:
            if s <= start and end <= e:
                shape = Shape(HEREDOC, owner_at(opener), names)
                break
        else:
            for s, e in quotes:
                if s <= start and end <= e:
                    shape = Shape(QUOTED, owner_at(start), names)
                    break
            else:
                # Executable position wins outright, and immediately.
                return Shape(CODE, owner_at(start), names)
        if best is None:
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
                f"owned by `{p.get('owner')}`, in a chain that executes nothing")

    def clears(self, command: str, kind: str, pattern: str) -> bool:
        """Whether this exemption clears a match of `pattern` in `command`."""
        if self.role != EXEMPT:
            return False
        if self.kind and self.kind != kind:
            return False
        if self.test == REGEX:
            try:
                return bool(re.search(self.pattern, command, re.IGNORECASE))
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
)

# Commands that must gate, forever, whatever anyone learns. Every proposed exemption is
# run against the whole set before it is allowed into the table; a proposal that would
# clear any of these is rejected and the rejection is recorded on the work order.
#
# The last two of the release group are not examples of anything a worker would type.
# They are there because they are the shapes a *learned* rule would most plausibly clear
# by accident: a heredoc body piped into a shell, and a quoted payload handed to `eval`.
# Both are the exact spans an exemption talks about, in the one arrangement where they
# are code after all.
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
    ("service_restart", "sudo systemctl restart jarvis-daemon"),
    ("service_restart", "systemctl --user stop jarvisd"),
    ("push_protected", "git push origin main"),
    ("push_protected", "git push -f origin HEAD:refs/heads/master"),
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


SEED_VERSION = "1"


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


def validate_pattern(pattern: str, command: str) -> str:
    """Why `pattern` may not be used as an exemption, or `""` if it may.

    The literal-anchor test is the one doing real work. A reviewer that answers with
    `.*`, `.+` or `git.*` is not describing a family of false positives, it is switching
    the gate off; requiring three consecutive literal characters costs a genuine rule
    nothing and makes the blanket cases unrepresentable.
    """
    pattern = pattern.strip()
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
        why = validate_pattern(exempt_pattern, command)
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
