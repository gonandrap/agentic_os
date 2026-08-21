"""The evidence packet: everything an independent validator reads, and nothing else.

A work order settles on its own word today. The validation panel replaces that with a
reviewer that never met the worker, and this module is the only thing standing between
the two: it assembles, from a work order's git worktree, the packet the seats read, and
it fingerprints that packet so a resubmission that produced nothing new can be told apart
from one that did.

## Why this module imports almost nothing

A seat's verdict is only worth something if the evidence under it was gathered by
something that cannot have been influenced by the thing being judged. So this is a leaf:
the standard library, and `worker_session` for the one pure path helper that knows where
a work order's worktree lives. No catalog, no store writes, no bus, no Neo, no panel,
nothing that reaches a model. `tests/test_evidence.py` asserts that import set by walking
this file's AST — including inside function bodies, because the house style is a lazy
import in the function that needs it, and a `sys.modules` check would miss those.

## The three rules that are the point of the module

**1. The fingerprint covers the FULL diff before truncation, and nothing but that and the
normalised `declared` text.** See `fingerprint`.

**2. `declared` is whitespace-normalised before hashing** — see `_normalise`. Re-running
the same tests and describing them with different line breaks is not new evidence.

**3. Truncation cuts at a file boundary, never mid-hunk**, and the names it removed go to
`dropped_files` while staying in `files`. A silently truncated diff read as complete is
how a security seat passes the file it never opened; `files` staying whole is what lets
a seat say "you claim tests were added and no path under tests/ appears here" even at a
limit that kept none of the patch.

## The merge-base ladder is pinned, not inferred

"Which branch is the default" has no obvious answer, and left to each collector it would
be guessed per project. The order is fixed in `_resolve_base` and the diff is the sum of
two commands, never one:

    with a base:  git diff <base>...HEAD   +   git diff HEAD
                  ─────────────────────       ──────────────
                  committed work              anything uncommitted

Both halves, concatenated. A worker that forgot to commit has still produced the change,
and dropping the second half is invisible in every test that only commits.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import worker_session

#: The truncation limit callers get when they do not pass one. It is a plain default on
#: `collect_work_order`, NOT a config read: this module has no opinion about the catalog,
#: and the round machine passes `os.validation.diff_chars` in.
DEFAULT_DIFF_CHARS = 60000


@dataclass(frozen=True)
class EvidencePacket:
    """One submission, as the panel sees it.

    `unit` and `children` are the seam for the feature-order collector that a later work
    order adds to this module: a feature's packet is the same shape with `unit="feature"`
    and one entry per merged child. For a work order `children` is always `()`.
    """

    unit: str                       # "work_order" | "feature"
    subject_id: str
    title: str
    description: str                # the unit's brief, verbatim
    summary: str                    # the submitter's --summary for this round
    declared: str                   # the submitter's --evidence text, verbatim
    pr_url: str                     # "" when none
    base: str                       # resolved merge-base ref, "" if unresolvable
    head: str                       # HEAD sha, "" if unresolvable
    stat: str                       # `git diff --stat` output
    files: tuple[str, ...]          # every changed path, NEVER truncated
    diff: str                       # unified diff, truncated to diff_chars
    diff_truncated: bool
    dropped_files: tuple[str, ...]  # whole files truncation removed; still in `files`
    #: sha256 of the diff BEFORE truncation. The packet deliberately does not keep that
    #: text: a packet is persisted per round and rendered into seat prompts, and a field
    #: holding the untruncated diff would be shipped by the first `asdict()` that builds
    #: one — defeating `diff_chars` silently. The digest is all `fingerprint` needs.
    diff_sha: str
    children: tuple[dict, ...] = ()


def fingerprint(packet: EvidencePacket) -> str:
    """A 16-char sha256 prefix over the FULL pre-truncation diff and the normalised
    `declared` text — and NOTHING else.

    Not `head`, not `base`, not `summary`, not `pr_url`. The fingerprint answers one
    question, "did this submitter produce new evidence?", and every field left out is a
    field a submitter can move without producing any:

    | a submitter that…                        | changes             | new evidence? |
    |------------------------------------------|---------------------|---------------|
    | adds an empty commit                     | `head`              | no            |
    | rewords its summary                      | `summary`           | no            |
    | re-runs the same tests, says so          | `declared` spacing  | no            |
    |   differently                            |                     |               |
    | opens a PR for work already submitted    | `pr_url`            | no            |
    | adds a test file                         | the diff            | **yes**       |
    | states a result it had not stated before | `declared` content  | **yes**       |

    Hashing `packet.diff` is the obvious implementation and it is wrong: the same tree
    would fingerprint differently at two truncation limits, which makes an integrity
    check depend on a display setting. `diff_sha` is taken before the cut for exactly
    that reason.
    """
    h = hashlib.sha256()
    h.update(packet.diff_sha.encode("utf-8"))
    h.update(b"\n")
    h.update(_normalise(packet.declared).encode("utf-8"))
    return h.hexdigest()[:16]


def collect_work_order(project_path: Path, wo: dict[str, Any], *, declared: str,
                       diff_chars: int = DEFAULT_DIFF_CHARS) -> EvidencePacket:
    """Assemble the packet for one work order from its worktree.

    Never raises for a repository that is missing, empty, broken or gone: a collector
    that throws would turn "the evidence is thin" into "the round crashed", and the
    empty packet (`files == ()`) is what the round machine escalates on.

    **When the worktree is gone the packet is empty, and git is NOT run anywhere else.**
    Falling back to the project root would diff the user's own checkout — whatever they
    happen to have open — and present it to the panel as this worker's evidence. That is
    a silent lie, and it is the one thing this function must never do.

    `wo` is a `work_orders` row. Note what is NOT read from it: `branch`. That column is
    declared and written by nothing in the codebase, so it is always NULL; the base comes
    from git, via the pinned ladder.
    """
    # type: ignore — `_ProjectRef` carries the one attribute that helper reads; see it.
    worktree = worker_session.worktree_path(_ProjectRef(project_path), wo)  # type: ignore[arg-type]
    base = head = stat = diff = ""
    files: tuple[str, ...] = ()
    if worktree is not None:
        base = _resolve_base(worktree)
        head = _git(worktree, "rev-parse", "HEAD").strip()
        # Committed work AND anything still uncommitted, whenever there is a base.
        ranges = (f"{base}...HEAD", None) if base else (None,)
        stat = "".join(_git(worktree, *_diff_args(r, "--stat")) for r in ranges)
        diff = "".join(_git(worktree, *_diff_args(r)) for r in ranges)
        files = _dedupe(
            name
            for r in ranges
            for name in _git(worktree, *_diff_args(r, "--name-only")).split("\n")
        )

    kept, truncated, dropped = _truncate(diff, diff_chars, files)
    return EvidencePacket(
        unit="work_order",
        subject_id=str(wo.get("id") or ""),
        title=str(wo.get("title") or ""),
        description=str(wo.get("description") or ""),
        summary=str(wo.get("result_summary") or ""),
        declared=declared,
        pr_url=str(wo.get("pr_url") or ""),
        base=base,
        head=head,
        stat=stat,
        files=files,
        diff=kept,
        diff_truncated=truncated,
        dropped_files=dropped,
        diff_sha=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    )


# --------------------------------------------------------------------------- internals


@dataclass(frozen=True)
class _ProjectRef:
    """The `.path` that `worker_session.worktree_path` reads, and nothing else.

    That helper is typed for a `catalog.ProjectSpec`, but it touches exactly one
    attribute, and importing the catalog here would drag config loading into a module
    whose whole value is that it depends on nothing. Collectors take a plain path.
    """

    path: Path


def _normalise(text: str) -> str:
    """Strip, then collapse every run of whitespace to a single space. Case preserved.

    `str.split()` with no argument splits on runs of any whitespace and discards the
    empties, so this is the whole rule in one expression.
    """
    return " ".join(text.split())


def _git(worktree: Path, *args: str) -> str:
    """Run one read-only git command in `worktree`. Any failure is "".

    Silent because every caller is on the evidence path: a repository with no commits,
    no `origin`, or no git at all yields a thinner packet, never an exception. Decoding
    replaces undecodable bytes rather than raising — a diff of a latin-1 source file
    must not be able to take the round down.
    """
    try:
        proc = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, errors="replace", check=False)
    except OSError:  # git not installed, worktree unreadable
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _diff_args(rev_range: str | None, *extra: str) -> tuple[str, ...]:
    """`git diff <range>` for the committed half, `git diff HEAD` for the working tree."""
    return ("diff", *extra, rev_range or "HEAD")


def _resolve_base(worktree: Path) -> str:
    """The pinned merge-base ladder. "" means rung 4: diff the working tree against HEAD.

    Rung 1 is the repository's own answer, which is why it comes first: `origin/HEAD` is
    what the remote says its default branch is, so a project on `master`, `trunk` or
    anything else is right without configuring Jarvis. The two guesses below it exist
    for the common case of a repo cloned without `--single-branch`, or one with no
    remote at all.
    """
    ref = _git(worktree, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    if ref:
        prefix = "refs/remotes/"
        return ref[len(prefix):] if ref.startswith(prefix) else ref
    for candidate in ("origin/main", "main"):
        if _git(worktree, "rev-parse", "--verify", "--quiet", candidate).strip():
            return candidate
    return ""


def _dedupe(names: Iterable[str]) -> tuple[str, ...]:
    """Union of the two halves' `--name-only` output, first appearance wins.

    A file changed in a commit AND left further modified in the working tree appears in
    both halves; it is one changed path, and `files` is a set of paths that happens to
    be ordered.
    """
    seen: dict[str, None] = {}
    for name in names:
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _sections(diff: str) -> list[tuple[str, str, str]]:
    """Split a unified diff into per-file sections: (new path, old path, text).

    The boundary is the `diff --git` header line and nothing else — NOT `@@`. A changed
    binary file's section carries no hunk at all ("Binary files a/x and b/x differ"), so
    an implementation that looks for hunk markers loses it, and no corpus of text diffs
    would ever show that.
    """
    sections: list[tuple[str, str, str]] = []
    new = old = ""
    buf: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if buf:
                sections.append((new, old, "".join(buf)))
            new, old = _header_paths(line)
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append((new, old, "".join(buf)))
    return sections


def _header_paths(line: str) -> tuple[str, str]:
    """`diff --git a/<old> b/<new>` → (new, old). Both, because of renames.

    `git diff --name-only` reports a rename under its NEW name, so that is the one that
    will match `files`; the old name is kept so the caller can fall back when a path git
    had to quote makes the b-side unparseable.
    """
    rest = line[len("diff --git "):].rstrip("\n")
    marker = rest.rfind(" b/")
    if rest.startswith("a/") and marker != -1:
        return rest[marker + 3:], rest[2:marker]
    return rest, rest


def _truncate(diff: str, limit: int,
              files: tuple[str, ...]) -> tuple[str, bool, tuple[str, ...]]:
    """Cut `diff` to `limit` characters at a file boundary. Returns (diff, cut?, dropped).

    Once one file is dropped every file after it is dropped too. Keeping a later section
    because it happened to fit would hand the panel a diff whose order no longer matches
    the repository's, which reads as complete and is not.
    """
    if len(diff) <= limit:
        return diff, False, ()
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for new, old, text in _sections(diff):
        if not dropped and used + len(text) <= limit:
            kept.append(text)
            used += len(text)
        else:
            # Prefer the name `files` knows: the b-side is that name in every case git
            # does not quote the path, and the a-side is the answer when it does.
            dropped.append(old if new not in files and old in files else new)
    return "".join(kept), True, _dedupe(dropped)
