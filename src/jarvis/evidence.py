"""The evidence packet: everything an independent validator reads, and nothing else (docs/superpowers/specs/2026-09-12-the-pull-request-is-the-artifact.md §3)"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import worker_session

log = logging.getLogger("jarvis.evidence")

#: The truncation limit callers get when they do not pass one
DEFAULT_DIFF_CHARS = 150000


@dataclass(frozen=True)
class EvidencePacket:
    """One submission, as the panel sees it"""

    unit: str                       # "work_order" | "feature"
    subject_id: str
    title: str
    description: str                # the unit's brief, verbatim
    summary: str                    # the submitter's --summary for this round
    declared: str                   # the submitter's --evidence text, verbatim
    pr_url: str                     # "" when none
    #: WHAT THESE TWO HOLD DEPENDS ON `source`, and the two meanings are not interchangeable
    base: str
    head: str
    stat: str                       # `git diff --stat` output
    files: tuple[str, ...]          # every changed path, NEVER truncated
    diff: str                       # unified diff, truncated to diff_chars
    diff_truncated: bool
    dropped_files: tuple[str, ...]  # whole files truncation removed; still in `files`
    #: sha256 of the diff BEFORE truncation
    diff_sha: str
    children: tuple[dict, ...] = ()
    #: Every assumption the work order ever filed, `{n, content, status}`, review state included (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §4.)
    assumptions: tuple[dict, ...] = ()
    #: A FEATURE's diff: `"children"`, or `"unscoped"` with no child merge commit (issue #985)
    diff_scope: str = ""
    #: Where `diff`, `files` and `stat` came from: `"pull_request"` or `"worktree"`
    source: str = "worktree"
    #: The pull request as a human reviewer reads it
    pr: dict[str, Any] | None = None
    #: Why the pull request could not be read, when `pr_url` was set and the fetch failed
    pr_error: str = ""
    #: Durable change that no diff can show, collected by `ops` and passed in
    side_effects: tuple[dict, ...] = ()
    #: sha256 over `side_effects`, computed at collection time exactly as `diff_sha` is
    side_effects_sha: str = ""
    #: The section of the feature's spec this unit was told to implement
    spec_ref: str = ""
    spec_section: str = ""
    #: WHAT EARLIER ROUNDS OF THIS SAME REVIEW ALREADY ASKED FOR
    history: tuple[dict, ...] = ()
    #: `((path, digest), ...)`, sorted
    file_shas: tuple[tuple[str, str], ...] = ()
    #: `(pr_head_sha, worktree_head)` when the PR lacks commits the worktree has; `()` otherwise
    pr_behind: tuple[str, ...] = ()
    #: THE FOUR COLUMNS A FIX ORDER CARRIES (docs/superpowers/specs/2026-09-27-self-evolution.md)
    gap_class: str = ""
    detector_id: str = ""
    remedy_rule_id: str = ""
    trigger_wo_id: str = ""
    #: The §7.3 checklist, RENDERED — static prose plus the four ids above
    self_evolution: str = ""
    #: THE §7.2 CHECKS THAT PASSED WITHOUT BEING EVALUATED
    self_evolution_unproven: tuple[str, ...] = ()


#: §7.3's checklist, in the PACKET and never in the system prompt: `validation.build_shared_prefix`...
SELF_EVOLUTION_CHECKLIST = """\
This work order is a FIX ORDER in the OS's self-evolution loop: it closes a class of gap
the OS keeps falling into by registering a detector and a remedy for it.

- gap class: {gap_class}
- detector: {detector_id}
- remedy rule: {remedy_rule_id}
- proved against: {trigger_wo_id}

{decided}{unproven}
The three questions, and they are about quality:

1. Does the condition match the real symptom, or a coincidence of this one order's
   state? Would it over-fire on a healthy order — name one if you can.
2. Is the remedy safe and idempotent for this condition, and is it the right primitive?
3. Is there a test that would fail if the condition stopped matching, and one that would
   fail if it started matching a healthy order?

The standing rule still governs: block only on a blocker. "The condition could also
check X" is a follow-up ticket, not a rejection."""

#: The mechanically-decided claim when ALL FOUR of §7.2's checks ran
ALL_CHECKS_DECIDED = """\
JUDGE QUALITY, NEVER EXISTENCE. That the detector exists, that it has a live remedy
naming a real primitive, that its condition actually fires on the triggering order's
recorded state, and that the remedy's preconditions hold against that same order were
all decided mechanically before this round was opened. Re-deciding any of them here
produces a blocker the submitter cannot act on.
"""

#: The same claim NARROWED to the checks that actually ran
SOME_CHECKS_DECIDED = """\
JUDGE QUALITY, NEVER EXISTENCE — WITH THE ONE EXCEPTION NAMED BELOW. Every check the OS
could run was decided mechanically before this round was opened, and re-deciding one of
those here produces a blocker the submitter cannot act on. The checks listed next are
NOT among them.
"""

#: What a seat is told about a check that passed unevaluated
UNPROVEN_CHECKS = """
THESE CHECKS WERE NOT RUN, AND THEY ARE YOURS TO RAISE. The OS could not decide them —
the reason is stated with each — so nothing mechanical stands behind them:

{notes}

Each of those, and only those, is the exception to "never existence". If the diff does
not satisfy you that such a check holds, say so: the alternative is a skip nobody sees.
"""


def self_evolution_checklist(gap_class: str, detector_id: str, remedy_rule_id: str,
                             trigger_wo_id: str,
                             unproven: Sequence[str] = ()) -> str:
    """The checklist for one fix order, or `""` for every order that is not one"""
    if not (gap_class or "").strip():
        return ""
    notes = tuple(str(n) for n in unproven if str(n).strip())
    return SELF_EVOLUTION_CHECKLIST.format(
        gap_class=gap_class.strip(),
        detector_id=detector_id or "none named — proposed, not yet registered",
        remedy_rule_id=remedy_rule_id or "none named",
        trigger_wo_id=trigger_wo_id or "no triggering order named",
        decided=ALL_CHECKS_DECIDED if not notes else SOME_CHECKS_DECIDED,
        unproven=("" if not notes else UNPROVEN_CHECKS.format(
            notes="\n".join(f"- {n}" for n in notes))))


def fingerprint(packet: EvidencePacket) -> str:
    """A 16-char sha256 prefix over the FULL pre-truncation diff (#200)"""
    h = hashlib.sha256()
    h.update(packet.diff_sha.encode("utf-8"))
    h.update(b"\n")
    h.update(packet.side_effects_sha.encode("utf-8"))
    h.update(b"\n")
    h.update(_normalise(packet.declared).encode("utf-8"))
    for a in packet.assumptions:
        h.update(b"\n")
        h.update(_normalise(str(a.get("content") or "")).encode("utf-8"))
    if packet.gap_class:
        # ONLY WHEN THERE IS ONE — the assumptions precedent above, and §7.3's whole instruction
        for value in (packet.gap_class, packet.detector_id, packet.remedy_rule_id,
                      packet.trigger_wo_id):
            h.update(b"\n")
            h.update(value.encode("utf-8"))
    return h.hexdigest()[:16]


def judged_head(packet: EvidencePacket) -> str:
    """WHICH COMMIT the panel is being shown, or `""` when nothing binds it to one (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §5.1)"""
    if packet.unit == "feature":
        return str(packet.head or "")
    if packet.source != "pull_request" or not packet.pr:
        return ""
    return str(packet.pr.get("head_sha") or "")


def ci_pending(packet: EvidencePacket) -> tuple[str, ...]:
    """The names of the checks GitHub has not finished running, newest packet only (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md §3.1)"""
    from . import github

    if packet.source != "pull_request" or not packet.pr:
        return ()
    return tuple(
        str(c.get("name") or "(unnamed check)")
        for c in (packet.pr.get("checks") or ())
        if str(c.get("status") or "").upper() in github.UNFINISHED_STATUSES)


def ci_failing(packet: EvidencePacket) -> tuple[str, ...]:
    """The names of the checks that have FINISHED and say the code is wrong (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md §3.1)"""
    from . import github

    if packet.source != "pull_request" or not packet.pr:
        return ()
    checks = tuple({"name": str(c.get("name") or ""),
                    "conclusion": str(c.get("conclusion") or "")}
                   for c in (packet.pr.get("checks") or ()))
    return github.failing_checks(checks)


#: Keys the REGISTRY stamps on an effect rather than the collector producing them (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §6.)
STAMPED_EFFECT_KEYS = frozenset({"attested"})


def side_effects_digest(side_effects: Iterable[dict[str, Any]]) -> str:
    """sha256 over a packet's side effects, stable against dict ordering"""
    effects = list(side_effects)
    if not effects:
        return ""
    h = hashlib.sha256()
    for effect in effects:
        for key in sorted(k for k in effect if k not in STAMPED_EFFECT_KEYS):
            h.update(f"{key}={effect[key]}\n".encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def file_digests(diff: str) -> tuple[tuple[str, str], ...]:
    """One digest per changed path, over that path's own section of `diff`"""
    out: dict[str, str] = {}
    for new, old, text in _sections(diff):
        path = new or old
        if not path:
            continue
        out[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return tuple(sorted(out.items()))


def changed_since(before: Mapping[str, str],
                  now: Mapping[str, str]) -> frozenset[str]:
    """Which paths moved between two rounds' `file_digests` maps"""
    return frozenset(
        {p for p, sha in now.items() if before.get(p) != sha}
        | {p for p in before if p not in now})


def nothing_to_judge(packet: EvidencePacket) -> str:
    """Is there anything here for a REVIEWER? (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §8.)"""
    if packet.files:
        return ""
    if not packet.side_effects:
        return "escalate"
    if packet.pr_url or packet.pr_error:
        return ""
    if all(e.get("attested") for e in packet.side_effects):
        return "void"
    return ""


def void_reason(packet: EvidencePacket) -> str:
    """Why this round was voided, in the words the record keeps"""
    what = "; ".join(str(e.get("summary") or e.get("kind") or "effect")
                     for e in packet.side_effects)
    return ("this submission changed no files, and everything it did deliver is an "
            "effect the OS verifies itself rather than one a reviewer can judge, so "
            f"no seat was asked: {what}")


def _history(rounds: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Prior rounds as a packet carries them"""
    return tuple({
        "round": int(r.get("round") or 0),
        "outcome": str(r.get("outcome") or ""),
        "reason": str(r.get("reason") or ""),
        "head_sha": str(r.get("head_sha") or ""),
        "blockers": tuple(
            {"title": str(b.get("title") or ""), "detail": str(b.get("detail") or "")}
            for b in (r.get("blockers") or ())),
    } for r in rounds)


def collect_work_order(project_path: Path, wo: dict[str, Any], *, declared: str,
                       diff_chars: int = DEFAULT_DIFF_CHARS,
                       spec: dict[str, str] | None = None,
                       side_effects: Iterable[dict[str, Any]] = (),
                       assumptions: Iterable[dict[str, Any]] = (),
                       history: Iterable[dict[str, Any]] = (),
                       unproven: tuple[str, ...] = ()) -> EvidencePacket:
    """Assemble the packet for one work order — from its PULL REQUEST when it has one"""
    pr_url = str(wo.get("pr_url") or "")
    pr_data: dict[str, Any] | None = None
    pr_error = source = ""
    base = head = stat = diff = ""
    files: tuple[str, ...] = ()
    behind: tuple[str, ...] = ()
    # type: ignore — `ProjectRef` carries the one attribute that helper reads.
    worktree = worker_session.worktree_path(ProjectRef(project_path), wo)  # type: ignore[arg-type]

    if pr_url:
        pr_data, pr_diff, pr_error = _pull_request(pr_url, project_path)
        if pr_data is not None:
            source = "pull_request"
            base, head = str(pr_data["base_ref"]), str(pr_data["head_ref"])
            stat, diff = str(pr_data["stat"]), pr_diff
            files = _dedupe(pr_data["files"])
            behind = _pr_behind(worktree, str(pr_data.get("head_sha") or ""))

    if source != "pull_request":
        source = "worktree"
        if worktree is not None:
            base = base_ref(worktree)
            head = _git(worktree, "rev-parse", "HEAD").strip()
            # Committed work AND anything still uncommitted, whenever there is a base
            ranges = (f"{base}...HEAD", None) if base else (None,)
            stat = "".join(_git(worktree, *_diff_args(r, "--stat")) for r in ranges)
            diff = "".join(_git(worktree, *_diff_args(r)) for r in ranges)
            files = _dedupe(
                name
                for r in ranges
                for name in _git(worktree, *_diff_args(r, "--name-only")).split("\n")
            )

    # §7.1's four columns, off the row this function already takes
    gap_class = str(wo.get("gap_class") or "")
    detector_id = str(wo.get("detector_id") or "")
    remedy_rule_id = str(wo.get("remedy_rule_id") or "")
    trigger_wo_id = str(wo.get("trigger_wo_id") or "")
    effects = tuple(dict(e) for e in side_effects)
    # BEFORE `_truncate`, and that ordering is the whole correctness of the map — see `file_digests`
    shas = file_digests(diff)
    kept, truncated, dropped = _truncate(diff, diff_chars, files)
    return EvidencePacket(
        unit="work_order",
        subject_id=str(wo.get("id") or ""),
        title=str(wo.get("title") or ""),
        description=str(wo.get("description") or ""),
        summary=str(wo.get("result_summary") or ""),
        declared=declared,
        pr_url=pr_url,
        base=base,
        head=head,
        stat=stat,
        files=files,
        diff=kept,
        diff_truncated=truncated,
        dropped_files=dropped,
        diff_sha=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        source=source,
        pr=pr_data,
        pr_error=pr_error,
        side_effects=effects,
        side_effects_sha=side_effects_digest(effects),
        spec_ref=_spec_ref(spec),
        spec_section=(spec or {}).get("section_text", ""),
        assumptions=tuple(
            {"n": int(a.get("n") or 0), "content": str(a.get("content") or ""),
             "status": str(a.get("status") or "pending")}
            for a in assumptions),
        history=_history(history),
        file_shas=shas,
        pr_behind=behind,
        gap_class=gap_class,
        detector_id=detector_id,
        remedy_rule_id=remedy_rule_id,
        trigger_wo_id=trigger_wo_id,
        self_evolution=self_evolution_checklist(gap_class, detector_id, remedy_rule_id,
                                                trigger_wo_id, unproven),
        self_evolution_unproven=tuple(unproven),
    )


def _pr_behind(worktree: Path | None, pr_head: str) -> tuple[str, ...]:
    """`(pr_head, worktree_head)` when the worktree has commits the PR lacks, else `()` (#704)"""
    if worktree is None or not pr_head:
        return ()
    wt_head = _git(worktree, "rev-parse", "HEAD").strip()
    # A PR head unknown here fails the rev-list and reads as "not behind": today's behaviour
    if wt_head and wt_head != pr_head and _git(
            worktree, "rev-list", "--max-count=1", f"{pr_head}..HEAD").strip():
        return (pr_head, wt_head)
    return ()


def _pull_request(url: str,
                  project_path: Path) -> tuple[dict[str, Any] | None, str, str]:
    """`(artifact-as-a-dict, diff, "")`, or `(None, "", why-not)`"""
    from . import github

    try:
        art = github.pr_artifact(url, cwd=project_path)
    except github.GitHubError as e:  # the packet gets the vocabulary, not the stderr
        return None, "", e.reason
    except Exception:  # noqa: BLE001 — a thin packet, never a dead round
        log.warning(
            "could not read the pull request at %s; falling back to the worktree",
            url, exc_info=True)
        return None, "", "the pull request could not be read"
    return {
        "url": art.url, "number": art.number, "title": art.title, "body": art.body,
        "state": art.state, "draft": art.draft, "base_ref": art.base_ref,
        # `head_sha` in the packet and on `validation_rounds`
        "head_ref": art.head_ref, "head_sha": art.head_oid,
        "additions": art.additions,
        "deletions": art.deletions, "files": list(art.files), "stat": art.stat,
        "checks": [dict(c) for c in art.checks],
    }, art.diff, ""


def _spec_ref(spec: dict[str, str] | None) -> str:
    """`<path> § <section>`, or "" when there is no resolved section to cite"""
    if not spec or not spec.get("section_text"):
        return ""
    return f"{spec.get('repo_path', '')} § {spec.get('section', '')}".strip()


def collect_feature(project_path: Path, fo: dict[str, Any], children: list[dict[str, Any]],
                    *, declared: str, summary: str = "",
                    diff_chars: int = DEFAULT_DIFF_CHARS,
                    side_effects: Iterable[dict[str, Any]] = (),
                    history: Iterable[dict[str, Any]] = ()) -> EvidencePacket:
    """Assemble the packet for one feature order, from the PROJECT ROOT"""
    base = str(fo.get("base_sha") or "")
    head = default_branch_head(project_path) if base else ""
    # Unscoped, this range carries every SIBLING feature merged into the same base (issue #985)
    commits = _dedupe(str(c.get("merge_commit") or "") for c in children)
    paths = _commit_paths(project_path, commits)
    stat = diff = ""
    files: tuple[str, ...] = ()
    if base and head:
        rng = f"{base}...{head}"
        scope = ("--", *paths) if paths else ()
        stat = _git(project_path, *_diff_args(rng, "--stat"), *scope)
        diff = _git(project_path, *_diff_args(rng), *scope)
        files = _dedupe(
            _git(project_path, *_diff_args(rng, "--name-only"), *scope).split("\n"))

    effects = tuple(dict(e) for e in side_effects)
    kept, truncated, dropped = _truncate(diff, diff_chars, files)
    return EvidencePacket(
        unit="feature",
        subject_id=str(fo.get("id") or ""),
        title=str(fo.get("title") or ""),
        description=str(fo.get("description") or ""),
        summary=summary,
        declared=declared,
        # A feature order never has a pull request of its own: its children each opened one and the user...
        pr_url="",
        base=base,
        head=head,
        stat=stat,
        files=files,
        diff=kept,
        diff_truncated=truncated,
        dropped_files=dropped,
        diff_sha=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        diff_scope="children" if paths else "unscoped",
        # `source` stays "worktree": a feature's diff is read from the project root and a feature order has...
        side_effects=effects,
        side_effects_sha=side_effects_digest(effects),
        children=tuple(
            {"id": str(c.get("id") or ""), "title": str(c.get("title") or ""),
             "summary": str(c.get("result_summary") or ""),
             "declared": str(c.get("declared") or ""),
             "merge_commit": str(c.get("merge_commit") or "")}
            for c in children),
        history=_history(history),
    )


def default_branch_head(repo: Path) -> str:
    """The sha the default branch points at right now, or "" if there is no answer"""
    ref = base_ref(repo)
    return _git(repo, "rev-parse", ref).strip() if ref else ""


def base_ref(worktree: Path) -> str:
    """The pinned merge-base ladder"""
    ref = _git(worktree, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    if ref:
        prefix = "refs/remotes/"
        return ref[len(prefix):] if ref.startswith(prefix) else ref
    for candidate in ("origin/main", "main"):
        if _git(worktree, "rev-parse", "--verify", "--quiet", candidate).strip():
            return candidate
    return ""


@dataclass(frozen=True)
class ProjectRef:
    """The `.path` that `worker_session.worktree_path` reads, and nothing else"""

    path: Path


# --------------------------------------------------------------------------- internals


def _normalise(text: str) -> str:
    """Strip, then collapse every run of whitespace to a single space"""
    return " ".join(text.split())


def _git(worktree: Path, *args: str) -> str:
    """Run one read-only git command in `worktree`"""
    try:
        proc = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, errors="replace", check=False)
    except OSError:  # git not installed, worktree unreadable
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _diff_args(rev_range: str | None, *extra: str) -> tuple[str, ...]:
    """`git diff <range>` for the committed half, `git diff HEAD` for the working tree"""
    return ("diff", *extra, rev_range or "HEAD")




def _commit_paths(repo: Path, commits: Iterable[str]) -> tuple[str, ...]:
    """Every path these commits touched, both names of a rename so a pathspec matches it"""
    # `<sha>^1` is the default branch before the merge, for a squash commit and a merge commit alike
    return _dedupe(
        name
        for sha in commits
        for name in _git(repo, "diff", "--name-only", "--no-renames",
                         f"{sha}^1", sha).split("\n"))


def _dedupe(names: Iterable[str]) -> tuple[str, ...]:
    """Union of the two halves' `--name-only` output, first appearance wins"""
    seen: dict[str, None] = {}
    for name in names:
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _sections(diff: str) -> list[tuple[str, str, str]]:
    """Split a unified diff into per-file sections: (new path, old path, text)"""
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
    """`diff --git a/<old> b/<new>` → (new, old)"""
    rest = line[len("diff --git "):].rstrip("\n")
    marker = rest.rfind(" b/")
    if rest.startswith("a/") and marker != -1:
        return rest[marker + 3:], rest[2:marker]
    return rest, rest


def _truncate(diff: str, limit: int,
              files: tuple[str, ...]) -> tuple[str, bool, tuple[str, ...]]:
    """Cut `diff` to `limit` characters at a file boundary (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
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
            # Prefer the name `files` knows: the b-side is that name in every case git does not quote the path
            dropped.append(old if new not in files and old in files else new)
    return "".join(kept), True, _dedupe(dropped)


