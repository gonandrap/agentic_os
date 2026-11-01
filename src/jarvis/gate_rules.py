"""The rule base behind the privileged-action gates — and how it learns (#194)"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .central_store import CentralStore

# -- vocabulary

MATCH = "match"
EXEMPT = "exempt"
CANARY = "canary"
ROLES = (MATCH, EXEMPT, CANARY)

# How a rule's `pattern` column is interpreted
REGEX = "regex"          # a regular expression
SIGNATURE = "signature"  # JSON: a structural shape, see `Shape`
COMMAND = "command"      # a literal command string (canaries only)
TESTS = (REGEX, SIGNATURE, COMMAND)

# Where in a command a gated literal turned up
CODE = "code"
HEREDOC = "heredoc"
QUOTED = "quoted"
EXEMPTIBLE_POSITIONS = (HEREDOC, QUOTED)

SOURCES = ("builtin", "neo", "user")

#: The worker's own merge, named rather than spelled: the one kind that can supply a pull request the...
PR_MERGE = "pr_merge"

#: The gate kind the OS files against ITSELF
SELF_HEAL = "self_heal"

#: The second kind nothing classifies into: the OS asking permission to merge a pull request the... (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §8.)
AUTO_MERGE = "auto_merge"


# -- what a gate IS (metadata; the patterns live in the table)


@dataclass(frozen=True)
class GateKind:
    """One class of privileged action"""

    name: str
    # What the action does, in the terms whoever reviews it needs
    summary: str
    # WHAT TO ASK THE WORKER FOR, for this kind
    why_ask: str = "why this action should proceed"
    evidence_ask: str = "what a reviewer can check"
    # Literals that, appearing in a project's `permissions.deny` rules
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
    # THE ONLY KIND NOTHING CLASSIFIES INTO, and the only one a project cannot switch off (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.)
    GateKind(
        name=SELF_HEAL,
        summary="let the supervisor act on a work order or feature order it judged "
                "unhealthy (this reaches a running session)",
        # No worker ever fills these in — `remedies.propose` writes the request itself
        why_ask="why acting is better than leaving the symptom with the user",
        evidence_ask="the health finding, the probe that raised it, and what the remedy "
                     "touches",
        conflict_markers=(),
    ),
    # THE SECOND KIND NOTHING CLASSIFIES INTO (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §8.)
    GateKind(
        name=AUTO_MERGE,
        summary="merge a pull request the validation panel has accepted, on the exact "
                "commit it judged (the OS performs this merge itself)",
        why_ask="why this diff is the one the panel passed, and why it may land now",
        evidence_ask="the round and its outcome, the commit the seats judged, and what "
                     "CI reports on that commit",
        conflict_markers=(),
    ),
)

KIND_NAMES = tuple(k.name for k in KINDS)
KIND_ORDER = {name: i for i, name in enumerate(KIND_NAMES)}


# -- reading a command

# Quoted spans are data, not code
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

# …with one exception: these hand their quoted payload back to a shell to execute
_SHELL_INVOKER = re.compile(
    r"\b(?:ba|z|k|da|a)?sh\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*c\b|\beval\b|\bxargs\b",
    re.IGNORECASE,
)

# …and so do these, on another host or in a container (#213)
_QUOTED_EXECUTOR = re.compile(
    r"(?<![\w./-])(?:ssh|(?:docker|podman|kubectl)(?:\s+[^\s|;&]+)*?\s+exec)(?![\w.-])",
)
_QUOTED_EXECUTORS = frozenset({"ssh", "docker exec", "podman exec", "kubectl exec"})


def _quoted_executors(text: str) -> frozenset[str]:
    """Which of `_QUOTED_EXECUTORS` `text` runs, in executable position"""
    found = (m.group(0).split()[0] for m in _QUOTED_EXECUTOR.finditer(_inert(text)))
    return frozenset(w if w == "ssh" else f"{w} exec" for w in found)

# A heredoc opener: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`
_HEREDOC_OPEN = re.compile(
    r"(?<!<)<<-?\s*(?:'(?P<sq>[^']+)'|\"(?P<dq>[^\"]+)\"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)

# Tools that read and cannot execute
_READERS = frozenset({
    "cat", "tac", "head", "tail", "nl", "wc", "ls", "stat", "file", "diff", "cmp",
    "grep", "egrep", "fgrep", "rg", "ag", "cut", "sort", "uniq", "tr", "column",
    "jq", "yq", "basename", "dirname", "realpath", "readlink", "echo", "printf",
    "pwd", "tree", "strings", "od", "xxd", "md5sum", "sha256sum", "cksum", "du", "df",
    "which", "sed",
})

# The mirror image, and the one list a learned rule can never talk its way around: things that RUN...
_EXECUTORS = frozenset({
    "sh", "bash", "zsh", "ksh", "dash", "ash", "eval", "exec", "source", ".",
    "xargs", "python", "python3", "ruby", "perl", "node", "php", "awk", "gawk",
    "find", "make", "uv",
})

# Wrappers that run whatever follows them without changing what it can do
_TRANSPARENT = frozenset({"command", "builtin", "time", "nice", "ionice", "sudo", "env"})

# Tools whose first word says nothing useful: `git` is not a thing you do, `git push` is
_SUBCOMMAND_TOOLS = frozenset({
    "git", "gh", "npm", "pnpm", "yarn", "uv", "pip", "poetry", "cargo", "docker",
    "systemctl", "jarvis", "kubectl", "brew", "apt", "gcloud", "aws",
})

# A substitution runs a command to build an argument
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")

# Where one command ends and the next begins
_SEPARATORS = re.compile(r"\|\||&&|[|;\n]|(?<![<>&])&")

# The subset of those that join INDEPENDENT commands — everything but the pipe (#194)
_LIST_SEPARATORS = re.compile(r"\|\||&&|[;\n]|(?<![<>&|])&(?!&)")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# What a redirection writes to
_REDIRECT_TARGET = re.compile(r"&\d+|[^\s<>&|;]+")
_DISCARD = ("/dev/null",)

# Every entry of `_READERS` that can write a file anyway
_MAY_WRITE = frozenset({"tee", "sort", "uniq", "yq", "sed"})

# …and the subset of those whose write is an IN-PLACE edit of the file named
_INPLACE = frozenset({"sed", "yq"})


def heredoc_spans(command: str) -> list[tuple[int, int, int, bool]]:
    """`(body_start, body_end, opener_start, terminated)` for every heredoc in `command` (#104)"""
    spans: list[tuple[int, int, int, bool]] = []
    cursor = 0
    for m in _HEREDOC_OPEN.finditer(command):
        # An opener inside an earlier heredoc's body is text, not an opener
        if any(s <= m.start() < e for s, e, _, _ in spans):
            continue
        delim = m.group("sq") or m.group("dq") or m.group("bare")
        # The body starts on the line after the opener
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
    """Replace `spans` with spaces, preserving length and newlines"""
    out = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(out), e)):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def owned_spans(command: str) -> list[tuple[int, int]]:
    """The span each heredoc occupies in `command`, as text belonging to its OWNER (#233)"""
    spans: list[tuple[int, int]] = []
    for start, end, _, _ in heredoc_spans(command):
        eol = command.find("\n", end)
        spans.append((start - 1, len(command) if eol == -1 else eol))
    return spans


def _inert(command: str) -> str:
    """`command` with every span the shell will not execute blanked, length preserved (#233)"""
    out = list(_QUOTED.sub(lambda m: " " * (m.end() - m.start()),
                           _blank(command, owned_spans(command))))
    for start, end in owned_spans(command):
        for i in range(max(0, start), min(len(out), end)):
            out[i] = " "
    return "".join(out)


def scannable(command: str) -> str:
    """The part of `command` that could actually *execute* something (#203)"""
    if _SUBSTITUTION.search(command):
        return command
    programs = program_spans(command)
    # Replace rather than delete, so neighbouring tokens can't fuse into a false match
    blanked = _QUOTED.sub(
        lambda m: m.group(0) if any(s <= m.start() and m.end() <= e for s, e in programs)
        else " ", command)
    if _SHELL_INVOKER.search(blanked) or _QUOTED_EXECUTOR.search(blanked):
        return command
    return blanked


def program_spans(command: str) -> list[tuple[int, int]]:
    """The heredoc bodies that are PROGRAMS rather than data: those owned by an executor"""
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
    """The name of the command a segment runs, `git commit` style, or `""`"""
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
    """`(start, end, name)` for each command in the chain, offsets into `command`"""
    inert = _inert(command)
    bounds: list[tuple[int, int]] = []
    pos = 0
    for m in _SEPARATORS.finditer(inert):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(command)))
    return [(s, e, _argv0(inert[s:e])) for s, e in bounds]


def command_names(command: str) -> frozenset[str]:
    """Every command the chain runs"""
    return frozenset(name for _, _, name in segments(command) if name)


def list_segments(command: str) -> list[tuple[int, int]]:
    """Spans of the independent commands in `command`, offsets into it"""
    inert = _inert(command)
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _LIST_SEPARATORS.finditer(inert):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(command)))
    return spans


def reads_only(command: str) -> bool:
    """True when every command in `command` can read but not execute"""
    # The asymmetry below is deliberate (#203)
    if _SUBSTITUTION.search(command):
        return False
    if any(not terminated for *_, terminated in heredoc_spans(command)):
        return False
    if _SHELL_INVOKER.search(_QUOTED.sub(" ", command)):
        return False
    # The INERT form, so a heredoc body stays with the command that owns it rather than splitting into a... (#233)
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
        # `sed -n '1,20p' f` reads; `sed -i s/a/b/ f` rewrites the file
        if name in _INPLACE and any(w.startswith("-i") or w == "--in-place"
                                    for w in words[1:]):
            return False
    return True


def _hands_off(segment: str) -> bool:
    """Whether `segment` could leave the thing it read where a later command finds it"""
    inert = _inert(segment)
    if command_names(segment) & _MAY_WRITE:
        return True
    if any(_ASSIGNMENT.match(w.lstrip("({")) for w in inert.split()):
        return True
    # The operator is located in the inert form, so a `>` inside a quoted argument is not a redirection
    for m in re.finditer(r">>?", inert):
        target = _REDIRECT_TARGET.match(segment[m.end():].lstrip())
        if target is None:
            return True
        name = target.group(0).strip("\"'")
        if not name.startswith("&") and name not in _DISCARD:
            return True
    return False


def read_only_mentions(command: str, pattern: str) -> bool:
    """True when every independent command that NAMES `pattern` can only read it (#194)"""
    return bool(_mentions_only(command, pattern, paperwork=False))


def mentions_only(command: str, pattern: str) -> str:
    """WHY nothing that names `pattern` could run it, or `""` if something could (#233)"""
    return _mentions_only(command, pattern, paperwork=True)


#: How each kind of segment fails to run what it names
_CLEARANCES = ("can read it and nothing more", "hand it to `jarvis gate` paperwork, "
               "which files a claim about it and runs nothing")


def _mentions_only(command: str, pattern: str, *, paperwork: bool) -> str:
    """The loop both carve-outs share"""
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
    # A reader that WRITES what it read hands the literal on by a route no parser here follows
    if handoff and not (reads_only(command) or (paperwork and all(
            reads_only(command[s:e]) or files_a_claim(command[s:e])
            for s, e in list_segments(command)))):
        return ""
    if grounds == {0}:
        return f"only in commands that {_CLEARANCES[0]}"
    if grounds == {1}:
        return f"only in commands that {_CLEARANCES[1]}"
    return f"only in commands that {_CLEARANCES[0]}, or that {_CLEARANCES[1]}"


#: The verbs that RECORD or READ a claim about a command
GATE_PAPERWORK_VERBS = frozenset({"request", "contest", "explain", "show", "list"})


def gate_paperwork(command: str) -> bool:
    """True when every command in the chain is the OS's own gate paperwork (docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §9.)"""
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
        # `jarvis` `gate` `<verb>` — anything shorter is not addressed to these verbs
        if len(words) < 3 or words[2] not in GATE_PAPERWORK_VERBS:
            return False
        seen = True
    return seen


def files_a_claim(segment: str) -> bool:
    """True when `segment` does nothing but RECORD or READ a claim about a command (#233)"""
    if _SUBSTITUTION.search(segment):
        return False
    return gate_paperwork(segment) or interpreter_paperwork(segment)


#: The `subprocess` entry points
_PY_RUNNERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

#: Everything else a filing script is allowed to call
_PY_INERT_FUNCS = frozenset({"open", "print", "str", "repr", "len", "int", "sorted",
                             "list", "tuple", "dict"})
#: …and these are methods on a VALUE — the result of a call, a literal, a subscript
_PY_INERT_METHODS = frozenset({"read", "read_text", "readlines", "strip",
                               "rstrip", "lstrip", "splitlines", "split", "join",
                               "format", "decode", "encode"})

#: The module-qualified calls that ARE allowed, named in full rather than by attribute
_PY_INERT_QUALIFIED = frozenset({("pathlib", "Path")})

#: What the shell rewrites inside an UNQUOTED heredoc body: parameter expansion
_EXPANDS = re.compile(r"[$`\\]")

#: `shell=True` turns argv into a command line and `executable=` replaces the program
_PY_RUN_KWARGS = frozenset({"capture_output", "text", "check", "cwd", "encoding",
                            "timeout", "stdout", "stderr", "stdin", "input"})

#: The whole grammar a filing script may be written in
_PY_NODES = (ast.Module, ast.Import, ast.alias, ast.Assign, ast.AnnAssign,
             ast.Expr, ast.Call, ast.keyword, ast.Name, ast.Attribute, ast.Constant,
             ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Load,
             ast.Store, ast.BinOp, ast.Add, ast.UnaryOp, ast.USub,
             ast.JoinedStr, ast.FormattedValue)


def interpreter_paperwork(segment: str) -> bool:
    """True when `segment` is an interpreter whose whole program only files paperwork (#233)"""
    if _SUBSTITUTION.search(segment):
        return False
    inert = _inert(segment).strip()
    words = inert.split()
    if not words or words[0] not in ("python", "python3"):
        return False
    # `-` is stdin, `<<PY` is the body it comes from
    if any(w != "-" and not w.startswith("<<") for w in words[1:]):
        return False
    spans = heredoc_spans(segment)
    if len(spans) != 1 or not spans[0][3]:
        return False
    start, end, opener, _ = spans[0]
    opened = _HEREDOC_OPEN.match(segment, opener)
    if opened is None:
        return False
    # `<<-` strips leading tabs, so the body python receives is not the one read here
    if segment[opener:opened.end()].startswith("<<-"):
        return False
    if opened.group("bare") is not None and _EXPANDS.search(segment[start:end]):
        return False
    return python_files_only_paperwork(segment[start:end])


def python_files_only_paperwork(program: str) -> bool:
    """True when every call in `program` is inert, and at least one files gate paperwork"""
    try:
        tree = ast.parse(program)
    except SyntaxError:
        return False
    nodes = list(ast.walk(tree))
    # Which names are MODULES
    modules = {a.name for n in nodes if isinstance(n, ast.Import) for a in n.names}
    # The module references a program IS allowed: the callee of a call `_py_call` accepts
    ok_attrs = {id(n.func) for n in nodes
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and _py_call(n) is not None}
    ok_attrs |= {id(n) for n in nodes
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and (n.value.id, n.attr) in _PY_INERT_QUALIFIED}
    ok_names = {id(n.value) for n in nodes
                if isinstance(n, ast.Attribute) and id(n) in ok_attrs}

    filed = False
    for node in nodes:
        if not isinstance(node, _PY_NODES):
            return False
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) \
                and isinstance(node.value, ast.Name) and node.value.id in modules \
                and id(node) not in ok_attrs:
            return False
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id in modules and id(node) not in ok_names:
            return False                      # `m = [os][0]` smuggles the module itself
        # Names only on the left of an `=`
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                return False
            # A name this checker TRUSTS may not be shadowed at all
            if any(t.id in _PY_INERT_FUNCS for t in targets
                   if isinstance(t, ast.Name)):
                return False
            if isinstance(node.value, (ast.Name, ast.Attribute)):
                return False
        # Same hole through the import: `import os as print`, `from os import system`
        if isinstance(node, ast.Import) and any(a.asname for a in node.names):
            return False
        if isinstance(node, ast.Call):
            verdict = _py_call(node)
            if verdict is None:
                return False
            filed = filed or verdict
    return filed


def _py_call(node: ast.Call) -> bool | None:
    """`True` a filing, `False` inert, `None` anything this cannot place — which gates"""
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
        # `open(p)` reads; `open(p, "w")` is a file the next command in the chain runs
        if func.id == "open" and (len(node.args) > 1 or node.keywords):
            return None
        return False
    if isinstance(func, ast.Attribute):
        # A bare name on the left is a MODULE, and `pickle.loads` is not `"x".strip`
        if isinstance(func.value, ast.Name):
            return False if (func.value.id, func.attr) in _PY_INERT_QUALIFIED else None
        return False if func.attr in _PY_INERT_METHODS else None
    return None


# -- the shape of a match


@dataclass(frozen=True)
class Shape:
    """Where a gated literal turned up in a command, and what owns that position"""

    position: str            # code | heredoc | quoted
    owner: str               # the command that owns that position, e.g. `git commit`
    names: frozenset[str]    # every command name in the match's own list segment
    handoff: bool = False    # the owner could leave the literal where a later command
                             # finds it — `_hands_off`

    @property
    def exemptible(self) -> bool:
        """Whether a shape like this may ever be cleared by a learned rule (#194)"""
        return (self.position in EXEMPTIBLE_POSITIONS
                and bool(self.owner)
                and not self.handoff
                and not self.blockers)

    def signature(self) -> str:
        return json.dumps({"position": self.position, "owner": self.owner},
                          sort_keys=True)

    @property
    def blockers(self) -> tuple[str, ...]:
        """What in the literal's own segment could run it"""
        return tuple(sorted(self.names & (_EXECUTORS | _QUOTED_EXECUTORS)))

    def unlearnable_reason(self) -> str:
        """Why no dismissal of this shape could become a rule, or `""` if one could"""
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
    """Where `pattern` matches inside `command`, or None if it does not occur there"""
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
        """What shares a command with the match"""
        for s, e in lists:
            if s <= offset < e:
                return command_names(command[s:e]) | _quoted_executors(command[s:e])
        return all_names | _quoted_executors(command)

    def handoff_at(offset: int) -> bool:
        """Whether the command holding the match writes what it was handed"""
        for s, e in lists:
            if s <= offset < e:
                return _hands_off(command[s:e])
        return True

    best: Shape | None = None
    for m in rx.finditer(command):
        start, end = m.span()
        for s, e, opener, terminated in heres:
            if s <= start and end <= e:
                # An unterminated body runs to the end of the string and swallows whatever follows it
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
                # Executable position wins outright, and immediately
                return Shape(CODE, owner_at(start), names_at(start),
                             handoff_at(start))
        # Among several non-code matches
        if best is None or (best.exemptible and not shape.exemptible):
            best = shape
    return best


# -- a rule


@dataclass(frozen=True)
class Rule:
    """One row of the rule base"""

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
        """The parsed `signature` body"""
        if self.test != SIGNATURE:
            return {}
        try:
            data = json.loads(self.pattern)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def render(self) -> str:
        """The pattern as a human reads it"""
        if self.test != SIGNATURE:
            return self.pattern
        p = self.payload
        return (f"a `{p.get('kind') or self.kind}` literal "
                f"{'in a heredoc body' if p.get('position') == HEREDOC else 'inside a quoted argument'} "
                f"owned by `{p.get('owner')}`, in a command that executes nothing")

    def clears(self, command: str, kind: str, pattern: str) -> bool:
        """Whether this exemption clears a match of `pattern` in `command`"""
        if self.role != EXEMPT:
            return False
        if self.kind and self.kind != kind:
            return False
        if self.test == REGEX:
            # One normalisation, then both tests read the same string
            subject = command.strip()
            # A regex exemption speaks about one line
            if "\n" in subject:
                return False
            try:
                # `fullmatch`, not `search`: an exemption argues that a command is harmless
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
    """A stable id for a seeded rule"""
    digest = hashlib.sha1(f"{role}|{kind}|{test}|{pattern}".encode()).hexdigest()
    return f"gr-{digest[:10]}"


# -- what the table is seeded with

# The recognisers
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
    ("config_write", r"\bjarvis\s+onboard\s+apply\b"),
)

# Commands that must gate, forever, whatever anyone learns
SEED_CANARIES: tuple[tuple[str, str], ...] = (
    ("pr_merge", "gh pr merge 31 --squash --delete-branch"),
    ("pr_merge", "gh pr merge --auto"),
    ("pr_merge", "gh api --method PUT repos/o/r/pulls/31/merge"),
    ("release", "./scripts/shipit.sh"),
    ("release", "bash scripts/shipit.sh --dry-run"),
    # The only release command the OS itself asks for since the 2026-10-01 spec §7
    ("release", "scripts/shipit.sh --stage --wo wo-1234abcd "
                "--base 442729f0c1e4b7a9d3f5068b2c4e7a1d9b0f3c58"),
    ("release", "gh release create jarvis-1.2.3"),
    ("release", "npm publish"),
    ("release", "uv publish"),
    ("release", "git push --follow-tags origin release/jarvis-1.2.3"),
    ("release", "cat <<'EOF' | bash\nscripts/shipit.sh\nEOF"),
    ("release", 'eval "bash scripts/shipit.sh"'),
    # Added with the issue #203 fix, to pin what that fix must NOT turn into a miss
    ("release", "python3 - <<'PY'\nscripts/shipit.sh\nPY"),
    # The pipe the reader exemption must never be loosened across, and the list separator it must be (#194)
    ("release", "cat scripts/shipit.sh | bash"),
    ("release", "ls -la .claude/skills/shipit/ && ./scripts/shipit.sh"),
    # …and the one that is neither: a reader whose output is WRITTEN somewhere the next command runs it
    ("release", "cat scripts/shipit.sh > /tmp/s.sh && bash /tmp/s.sh"),
    # The same, run without naming any member of `_EXECUTORS` at all
    ("release", "cat scripts/shipit.sh > /tmp/s.sh && chmod +x /tmp/s.sh && /tmp/s.sh"),
    # A remote or container shell runs its quoted payload (#213)
    ("release", 'ssh prod "scripts/shipit.sh"'),
    ("pr_merge", 'docker exec -it c "gh pr merge 31"'),
    ("service_restart", "sudo systemctl restart jarvis-daemon"),
    ("service_restart", "systemctl --user stop jarvisd"),
    ("push_protected", "git push origin main"),
    ("push_protected", "git push -f origin HEAD:refs/heads/master"),
    # The recursion this cuts: the console can turn gates off
    ("config_write", "jarvis config set proj_a worker.permission_mode bypassPermissions "
                     "--reason \"faster\""),
    ("config_write", "jarvis config unset os.validation.enabled --reason \"noisy\""),
    ("config_write", "jarvis config restore cfg-0123456789abcdef --reason \"revert\""),
    ("config_write", "jarvis config adopt --reason \"hand edit\""),
    ("config_write", "jarvis onboard apply --from /tmp/draft.json"),
    # One per kind: a reader, a newline, and the canonical gated command
    ("release", "echo hi\n./scripts/shipit.sh"),
    ("pr_merge", "cat README.md\ngh pr merge 31 --squash"),
    ("service_restart", "cat README.md\nsudo systemctl restart jarvis-daemon"),
    ("push_protected", "ls -la\ngit push origin main"),
    ("config_write", "head -5 README.md\njarvis config set p worker.model haiku "
                     "--reason \"cheaper\""),
)


def seed_rows() -> list[dict[str, Any]]:
    """Every seeded rule, as store rows"""
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


# 2: the `config_write` kind, its recogniser and its canaries (the config console). 3: the... (#194)

# 6: `jarvis onboard apply` writes configuration too, so it joins `config_write`
SEED_VERSION = "6"


# -- the live rule base


@dataclass(frozen=True)
class Match:
    """A recogniser fired, and nothing cleared it"""

    kind: str
    pattern: str
    rule_id: str


@dataclass(frozen=True)
class Decision:
    """The classifier's full reasoning, for the gate and for anyone debugging one"""

    match: Match | None
    cleared: tuple[tuple[str, str, str], ...] = ()  # (rule_id, kind, pattern)
    trace: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleSet:
    """The rules in force"""

    rules: tuple[Rule, ...] = ()

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> RuleSet:
        return cls(rules=tuple(Rule.from_row(r) for r in rows))

    @classmethod
    def from_seeds(cls) -> RuleSet:
        """The seeds, as a RuleSet"""
        return cls.from_rows(seed_rows())

    @classmethod
    def load(cls, central: CentralStore | None = None) -> RuleSet:
        """The rules as the database holds them, seeding it first if it is new"""
        from .central_store import CentralStore as _CS

        store = central or _CS()
        try:
            return cls.from_rows(store.gate_rules())
        finally:
            if central is None:
                store.close()

    # -- views

    def of_role(self, role: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.role == role)

    def matchers(self) -> tuple[Rule, ...]:
        """Recognisers, in gate order — so that a command tripping two gates is reported as the same one it..."""
        return tuple(sorted(self.of_role(MATCH),
                            key=lambda r: (KIND_ORDER.get(r.kind, 99), r.ts, r.id)))

    def exemptions(self) -> tuple[Rule, ...]:
        return self.of_role(EXEMPT)

    def canaries(self) -> tuple[Rule, ...]:
        return self.of_role(CANARY)

    def with_rule(self, rule: Rule) -> RuleSet:
        return RuleSet(rules=(*self.rules, rule))

    # -- classification

    def decide(self, command: str, enabled: Iterable[str],
               extra_patterns: dict[str, tuple[str, ...]] | None = None) -> Decision:
        """Which gate `command` trips, why, and what cleared it if nothing did"""
        live = set(enabled)
        if not command or not live:
            return Decision(None, trace=("no gate is enabled",))
        if reads_only(command):
            return Decision(None, trace=("every command in the chain can only read",))
        # Before the table, because no entry in the table can state it — §9
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
        # Catalog-supplied patterns stay project config rather than learned state: they describe THIS repo's...
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

    # -- the safety net

    def check_canaries(self, candidate: Rule | None = None) -> list[dict[str, str]]:
        """Canaries that would STOP gating"""
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


# -- learning from a dismissal

# A reviewer-authored pattern has to survive this before it is allowed to clear anything
_MAX_PATTERN = 400
_LITERAL_ANCHOR = re.compile(r"[A-Za-z0-9_]{3,}")


def _spans_a_newline(rx: re.Pattern[str], command: str) -> bool:
    """Whether `rx` would still clear `command` with a gated command appended below it"""
    probes = [gated for _, gated in SEED_CANARIES if "\n" not in gated]
    if not probes:
        raise AssertionError(
            "no single-line canary to probe with: the newline floor would pass "
            "everything. SEED_CANARIES must keep at least one single-line command.")
    return any(rx.search(f"{command}\n{gated}") for gated in probes)


def validate_pattern(pattern: str, command: str) -> str:
    """Why `pattern` may not be used as an exemption, or `""` if it may"""
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
    """What the OS decided to learn from one dismissal, and what it refused to learn"""

    rule: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def learned(self) -> bool:
        return self.rule is not None


def propose_exemption(ruleset: RuleSet, *, command: str, kind: str, pattern: str,
                      reason: str = "", exempt_pattern: str = "",
                      approval_id: int | None = None, wo_id: str = "",
                      project: str = "", rule_id: str = "") -> Proposal:
    """Turn a dismissal into a rule, or explain why it could not become one"""
    proposal = Proposal()
    shape = shape_of(command, pattern)

    def already_have(rule: Rule) -> bool:
        """Whether an identical rule is already in force"""
        return any(r.test == rule.test and r.pattern == rule.pattern
                   and r.kind == rule.kind for r in ruleset.exemptions())

    if exempt_pattern.strip():
        # A multi-line command generalises into a regex only by deciding which of its lines mattered
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
