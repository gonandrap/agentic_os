"""The evidence packet: everything an independent validator reads, and nothing else.

A work order settles on its own word today. The validation panel replaces that with a
reviewer that never met the worker, and this module is the only thing standing between
the two: it assembles the packet the seats read, and it fingerprints that packet so a
resubmission that produced nothing new can be told apart from one that did.

Two collectors, one packet. `collect_work_order` reads THE PULL REQUEST when there is
one and the work order's own worktree when there is not; `collect_feature` reads the
project root, because the question a feature-level panel exists to answer — do these
children add up, and do they collide — is only answerable where the children's work has
actually met.

## The pull request is the artifact

A work order that ends behind a pull request has already assembled its own evidence
there, for exactly this purpose: the diff, the reasoning, the screenshots, what CI said.
So that is what the panel judges, the way a human judges one — see
docs/superpowers/specs/2026-09-12-the-pull-request-is-the-artifact.md §3 for the
three-row table this collector implements, and §2 for why the fetch lives in a
read-only module rather than in the seats' hands.

**A pull request that cannot be read never silently becomes a worktree packet.**
`pr_error` carries the reason and the seat prompt prints it. Presenting the worktree as
the pull request would be the same silent lie this function already refuses to tell when
the worktree is missing.

## Why this module imports almost nothing

A seat's verdict is only worth something if the evidence under it was gathered by
something that cannot have been influenced by the thing being judged. So this is a leaf:
the standard library, `worker_session` for the one pure path helper that knows where a
work order's worktree lives, and `github` for the pull-request read — which qualifies on
the same rule, being a module that can only ask GitHub questions and never tell it
anything. No catalog, no store writes, no bus, no Neo, no panel, nothing that reaches a
model. `tests/test_evidence.py` asserts that import set by walking this file's AST —
including inside function bodies, because the house style is a lazy import in the
function that needs it, and a `sys.modules` check would miss those.

Note what that rules OUT and why it costs nothing: `side_effects` is collected by `ops`
and passed in, exactly as `spec` and `children` are, because the durable non-file change
a work order made lives in a DATABASE and this module may not open one.

## The three rules that are the point of the module

**1. The fingerprint covers the FULL diff before truncation, the side effects, the
assumptions the submitter filed, and nothing but those and the normalised `declared`
text.** See `fingerprint`.

**2. `declared` is whitespace-normalised before hashing** — see `_normalise`. Re-running
the same tests and describing them with different line breaks is not new evidence.

**3. Truncation cuts at a file boundary, never mid-hunk**, and the names it removed go to
`dropped_files` while staying in `files`. A silently truncated diff read as complete is
how a security seat passes the file it never opened; `files` staying whole is what lets
a seat say "you claim tests were added and no path under tests/ appears here" even at a
limit that kept none of the patch.

## The merge-base ladder is pinned, not inferred

"Which branch is the default" has no obvious answer, and left to each collector it would
be guessed per project. The order is fixed in `base_ref` and the diff is the sum of
two commands, never one:

    with a base:  git diff <base>...HEAD   +   git diff HEAD
                  ─────────────────────       ──────────────
                  committed work              anything uncommitted

Both halves, concatenated. A worker that forgot to commit has still produced the change,
and dropping the second half is invisible in every test that only commits.

The SAME ladder resolves a feature's head (`default_branch_head`), one step further to a
sha. A feature has no second half: see `collect_feature`.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import worker_session

log = logging.getLogger("jarvis.evidence")

#: The truncation limit callers get when they do not pass one. It is a plain default on
#: `collect_work_order`, NOT a config read: this module has no opinion about the catalog,
#: and the round machine passes `os.validation.diff_chars` in. Kept level with
#: `catalog.DEFAULT_VALIDATION_DIFF_CHARS`, which is where the number was measured.
DEFAULT_DIFF_CHARS = 150000


@dataclass(frozen=True)
class EvidencePacket:
    """One submission, as the panel sees it.

    `unit` and `children` are what tell the two collectors apart: a feature's packet is
    the same shape with `unit="feature"` and one entry per merged child. For a work order
    `children` is always `()`.
    """

    unit: str                       # "work_order" | "feature"
    subject_id: str
    title: str
    description: str                # the unit's brief, verbatim
    summary: str                    # the submitter's --summary for this round
    declared: str                   # the submitter's --evidence text, verbatim
    pr_url: str                     # "" when none
    #: WHAT THESE TWO HOLD DEPENDS ON `source`, and the two meanings are not
    #: interchangeable. On the worktree path they are git objects — a resolved
    #: merge-base ref and a HEAD sha. On the pull-request path they are the PR's
    #: BRANCH NAMES (`baseRefName`/`headRefName`), because that is what GitHub reports
    #: and what a reviewer reading the PR sees; a sha would need another round trip to
    #: learn something nobody asked for. Both are "" when unresolvable.
    #:
    #: So anything that wants to RESOLVE these — `git rev-parse`, a range like
    #: `base...head`, a sha comparison between rounds — must check `source` first. They
    #: are rendered, never resolved: `validation.build_packet_prompt` prints them under
    #: a heading that names which source they came from, and `fingerprint` deliberately
    #: excludes both.
    base: str
    head: str
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
    #: Every assumption the work order ever filed, `{n, content, status}`, review state
    #: included — a decision the user accepted in round 1 is still embodied in the diff
    #: round 2 is judging. `()` for a feature order, which files none.
    #: See docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §4.
    assumptions: tuple[dict, ...] = ()
    #: Where `diff`, `files` and `stat` came from: `"pull_request"` or `"worktree"`. A
    #: seat is told which, because "this is the pull request" and "this is what was
    #: lying in a worktree" are different claims about the same bytes.
    source: str = "worktree"
    #: The pull request as a human reviewer reads it — title, body, state, draft, refs,
    #: additions/deletions, check runs — or None when there is none, or none readable.
    #: A dict rather than the `github.PullRequestArtifact` so nothing downstream needs
    #: that type to render a packet. THE DIFF IS NOT A KEY IN HERE AND NEVER WAS: it is
    #: returned separately by `_pull_request`, so the rule that a packet carries no
    #: untruncated diff (kn-c8b9c7da) holds by construction rather than by a caller
    #: remembering to remove it.
    pr: dict[str, Any] | None = None
    #: Why the pull request could not be read, when `pr_url` was set and the fetch
    #: failed. Non-empty means the diff below is the WORKTREE's and the submitter
    #: pointed at something else.
    pr_error: str = ""
    #: Durable change that no diff can show, collected by `ops` and passed in. One dict
    #: per effect: `{"kind", "id", "summary", "detail", "attested"}`. Empty is the normal
    #: case. `attested` is stamped by the collector REGISTRY, never by the collector, and
    #: is what `nothing_to_judge` reads — see it, and `STAMPED_EFFECT_KEYS`.
    side_effects: tuple[dict, ...] = ()
    #: sha256 over `side_effects`, computed at collection time exactly as `diff_sha` is,
    #: and hashed into `fingerprint` for exactly the same reason — see that function.
    side_effects_sha: str = ""
    #: The section of the feature's spec this unit was told to implement — `spec_ref` is
    #: "<path> § <section>" for citing, `spec_section` its text. Both "" for a standalone
    #: work order and for anything planned before specs existed, which is the null case
    #: the panel already handles: no section, no section heading in the prompt.
    #: DELIBERATELY NOT in `fingerprint`: the spec does not change between rounds, so
    #: hashing it would only make an unchanged submission look new after a spec edit.
    spec_ref: str = ""
    spec_section: str = ""
    #: WHAT EARLIER ROUNDS OF THIS SAME REVIEW ALREADY ASKED FOR — BLOCKERS ONLY, which
    #: is what the key inside each entry is called. One entry per prior round:
    #: `{"round", "outcome", "reason", "head_sha", "blockers"}`, oldest first.
    #:
    #: **A FOLLOW-UP MAY NOT ENTER THIS FIELD.** `validation._run_chair` hands the chair
    #: the SAME shared prefix the four seats read, so anything in here is in front of the
    #: chair — and a prior round's follow-ups are exactly what the chair is deliberately
    #: not shown. Spec 2026-09-15-the-panel-blocks-on-blockers.md §5.2.1, which also says
    #: what that gives up and why the alternative (a prefix of the chair's own) is worse.
    #:
    #: Passed in like `side_effects`, never looked up: the entries come from
    #: `validation_rounds` and `validation_opinions` and this module may not open a store.
    #: DELIBERATELY NOT in `fingerprint` — §5.5, and that function's exclusion table.
    history: tuple[dict, ...] = ()
    #: `((path, digest), ...)`, sorted — ONE DIGEST PER CHANGED FILE, over that file's
    #: own section of the UNTRUNCATED diff. `packet.files` cannot answer "what moved
    #: since the last round": every field in this packet is cumulative against the base,
    #: so a round that edits one file it already touched has the identical file list.
    #: Comparing two rounds' maps is what `validation.unanswered` needs, and a round
    #: persists this beside `head_sha` for the next one to read.
    #:
    #: A tuple of pairs rather than a dict because the packet is frozen and every other
    #: field here is hashable. DELIBERATELY NOT in `fingerprint`: `diff_sha` already
    #: covers exactly the same bytes, so hashing it in would add nothing and double-count.
    file_shas: tuple[tuple[str, str], ...] = ()


def fingerprint(packet: EvidencePacket) -> str:
    """A 16-char sha256 prefix over the FULL pre-truncation diff, the side effects and
    the normalised `declared` text — and NOTHING else.

    Not `head`, not `base`, not `summary`, not `pr_url`. THAT EXCLUSION LIST IS
    UNCHANGED, and saying so by name is the point: `side_effects_sha` joined the hash
    (Neo, question 253, correcting question 133 — kn-c8b9c7da) without letting anything
    else in. The fingerprint answers one question, "did this submitter produce new
    evidence?", and every field left out is a field a submitter can move without
    producing any:

    | a submitter that…                        | changes             | new evidence? |
    |------------------------------------------|---------------------|---------------|
    | adds an empty commit                     | `head`              | no            |
    | rewords its summary                      | `summary`           | no            |
    | re-runs the same tests, says so          | `declared` spacing  | no            |
    |   differently                            |                     |               |
    | opens a PR for work already submitted    | `pr_url`            | no            |
    | adds a test file                         | the diff            | **yes**       |
    | states a result it had not stated before | `declared` content  | **yes**       |
    | retracts a DIFFERENT knowledge entry     | `side_effects`      | **yes**       |
    | files an assumption it had not filed     | assumption text     | **yes**       |
    | has an assumption ACCEPTED by the user   | assumption `status` | no            |
    | is judged after an earlier round         | `history`           | no            |

    The side-effects row is why the formula widened once. Without it, two consecutive
    diff-less rounds retracting two different entries hash identically, and
    `_preceding_round` escalates round 2 as "identical to round 1" — issue #200
    reappearing one guard further along, with the empty-diff guard already fixed
    (spec 2026-09-12-the-pull-request-is-the-artifact.md §5).

    The assumptions are mixed in ONLY when there are any, so every work order that files
    none hashes exactly as it did before that existed. Their review STATE is left out on
    the same reasoning as the rest of the table: the user accepting an assumption is not
    the submitter producing evidence, and hashing it would make an unchanged resubmission
    look new (spec 2026-09-13-two-gates-not-a-chain.md §4).

    The history row is the same rule from the other end: `history` is written by the OS,
    not by the submitter, and it differs every round BY CONSTRUCTION. Hashing it would
    make every unchanged resubmission look like new evidence — silently disabling
    `Daemon._preceding_round`'s repeat guard, which is the one thing here that catches a
    submitter that changed nothing — and would change the hash of every round already
    stored (spec 2026-09-15-the-panel-blocks-on-blockers.md §5.5).

    **Do not add `head` to this in order to answer "which commit did the panel judge".**
    That question has its own function, `judged_head`, and its own column, because the
    first row of the table above is the two questions disagreeing: folding `head` in here
    would make every unchanged resubmission look like new evidence, break
    `Daemon._preceding_round`'s repeat guard, and change the hash of every round already
    on the record.

    Hashing `packet.diff` is the obvious implementation and it is wrong: the same tree
    would fingerprint differently at two truncation limits, which makes an integrity
    check depend on a display setting. `diff_sha` is taken before the cut for exactly
    that reason, and `side_effects_sha` at the same moment for the same one.
    """
    h = hashlib.sha256()
    h.update(packet.diff_sha.encode("utf-8"))
    h.update(b"\n")
    h.update(packet.side_effects_sha.encode("utf-8"))
    h.update(b"\n")
    h.update(_normalise(packet.declared).encode("utf-8"))
    for a in packet.assumptions:
        h.update(b"\n")
        h.update(_normalise(str(a.get("content") or "")).encode("utf-8"))
    return h.hexdigest()[:16]


def judged_head(packet: EvidencePacket) -> str:
    """WHICH COMMIT the panel is being shown, or `""` when nothing binds it to one.

    The question `fingerprint` deliberately cannot answer, and must not be changed to —
    see that function's exclusion table, whose first row is "adds an empty commit →
    changes `head` → no new evidence". That is right for *did this submitter produce new
    evidence* and exactly wrong for *which commit did the seats read*: the two have
    opposite answers on an empty commit, so one field cannot serve both
    (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §5.1).

    `""` IS THE FAIL-CLOSED VALUE and it reads as "not recorded", never as "matches". A
    worktree packet gets it because a local diff with no pull request behind it — or one
    whose PR fetch failed and fell back (`packet.pr_error`) — binds the verdict to
    nothing a remote can be held to.

    NOT `packet.head`: on the pull-request path that field holds `headRefName`, a BRANCH
    NAME, which is precisely the thing that keeps meaning something different as commits
    land on it.
    """
    if packet.source != "pull_request" or not packet.pr:
        return ""
    return str(packet.pr.get("head_sha") or "")


def ci_pending(packet: EvidencePacket) -> tuple[str, ...]:
    """The names of the checks GitHub has not finished running, newest packet only.

    `()` — nothing to wait for — covers BOTH green answers and the two that are not
    answers at all: a repository that runs no checks, and a packet with no pull request
    behind it. Neither can ever become a verdict, so a round that waited on one would
    wait for ever; `daemon._validate_work_order` reads "wait" from this and must not be
    handed a wait nothing can end.

    WHY THIS EXISTS. Until 2026-09-18 a worker proved its own suite locally and declared
    the number, which the panel then had to take on the submitter's word — and could not,
    because `validator-seats/tester.md` judges the declared evidence against the pull
    request's check runs. A worker that stops running the suite (the user's ruling, and
    kn-356c724b) has nothing to declare until CI reports, so the OS waits for CI on the
    worker's behalf rather than putting the wait inside a worker turn where it would sit
    blocked for twenty minutes and re-write the whole conversation at the cache-WRITE
    rate on the next call (wo-16a488ee: seven such re-writes, ~1.5M tokens).

    Reading `status` and not `conclusion` is the whole of the distinction: an
    unfinished check has no conclusion yet, and `failing_checks` — which reads
    `conclusion` — is deliberately silent about it. A RED check is NOT pending: the
    answer is in, it is bad, and the round should open so a seat can say so.
    """
    from . import github

    if packet.source != "pull_request" or not packet.pr:
        return ()
    return tuple(
        str(c.get("name") or "(unnamed check)")
        for c in (packet.pr.get("checks") or ())
        if str(c.get("status") or "").upper() in github.UNFINISHED_STATUSES)


#: Keys the REGISTRY stamps on an effect rather than the collector producing them — the
#: OS classifying an effect, not a submitter delivering one. Excluded from the digest on
#: `history`'s rule; spec docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §6.
STAMPED_EFFECT_KEYS = frozenset({"attested"})


def side_effects_digest(side_effects: Iterable[dict[str, Any]]) -> str:
    """sha256 over a packet's side effects, stable against dict ordering.

    `""` for none, NOT the sha of the empty string: every packet collected before this
    field existed carries `""`, and a round that genuinely has no side effects must
    fingerprint the same as one of those. Giving "nothing" a non-empty digest would
    change every existing fingerprint and make the next round of every open work order
    read as new evidence.

    `STAMPED_EFFECT_KEYS` are skipped for the second half of that same sentence: they
    arrived after the field did, and hashing them would change the digest of every
    knowledge effect already collected — making the next round of every open work order
    read as new evidence and silently disabling `Daemon._repeat_submission`.
    """
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
    """One digest per changed path, over that path's own section of `diff`.

    Called with the UNTRUNCATED diff and nowhere else: a map built from the truncated
    text would say the dropped files stopped changing, which is the one wrong answer
    `validation.unanswered` must never be given — it would read as "the submitter
    touched nothing" and bounce a round that did the work.

    Keyed on the NEW path, falling back to the old one, exactly as `_dedupe` keys
    `packet.files` — so a path in this map and a path in that tuple are the same string.
    A rename therefore reads as a change to the new path, which is what it is.
    """
    out: dict[str, str] = {}
    for new, old, text in _sections(diff):
        path = new or old
        if not path:
            continue
        out[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return tuple(sorted(out.items()))


def changed_since(before: Mapping[str, str],
                  now: Mapping[str, str]) -> frozenset[str]:
    """Which paths moved between two rounds' `file_digests` maps.

    A path whose digest differs, a path only `now` has, AND a path only `before` had —
    reverting a file is a change to it, and a submitter answering "delete this" would
    otherwise look like it did nothing.
    """
    return frozenset(
        {p for p, sha in now.items() if before.get(p) != sha}
        | {p for p in before if p not in now})


def nothing_to_judge(packet: EvidencePacket) -> str:
    """Is there anything here for a REVIEWER? `""` yes, else `"void"` or `"escalate"`.

    THE ONE HOME of the empty-packet rule, called by both of `daemon.py`'s validation
    loops. Two copies is how a feature order whose children were releases keeps
    escalating after the work-order guard has been fixed — spec
    docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §8.

    | files | side effects                | answer                                 |
    |-------|-----------------------------|----------------------------------------|
    | any   | any                         | `""` — the diff is the review          |
    | none  | none                        | `"escalate"` — THE GUARD, unchanged    |
    | none  | at least one NOT `attested` | `""` — issue #200's case, unchanged     |
    | none  | all `attested`, no PR       | `"void"`                               |

    **`"void"` IS DERIVED AND NEVER CHOSEN.** No seat returns it and no validator verdict
    produces it: it is decided here, from the packet, before any seat is called — and a
    submitter cannot reach it by delivering nothing, because that is row 2. `attested` is
    computed by `ops`'s collector registry and defaults to False, so a collector added
    tomorrow that has not thought about this gets JUDGED, never silently voided (§3).

    **A UNIT THAT POINTED AT A PULL REQUEST IS NEVER VOIDED**, whatever its effects.
    `packet.files` is the PR's when the PR could be read and the WORKTREE's when it could
    not (`pr_error`), so a release-shaped packet with an unreadable pull request beside it
    would otherwise void — and `ops.land_when_cleared` would park that pull request on the
    merge queue with nobody having read a line of it (review round 1). A release has no
    pull request, so this costs the case void exists for nothing.
    """
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
    """Why this round was voided, in the words the record keeps. One home, two loops."""
    what = "; ".join(str(e.get("summary") or e.get("kind") or "effect")
                     for e in packet.side_effects)
    return ("this submission changed no files, and everything it did deliver is an "
            "effect the OS verifies itself rather than one a reviewer can judge, so "
            f"no seat was asked: {what}")


def _history(rounds: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Prior rounds as a packet carries them, normalised HERE so that no caller decides
    the shape and every packet reads the same whichever collector built it.

    **`severity` is dropped and the key is `blockers`.** The caller has already filtered
    with `validation.blockers()`; keeping a severity word would invite a later reader to
    filter again, and the second filter is the one that disagrees with the first —
    which, for this field, means follow-up text in front of the chair (§5.2.1).
    """
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
                       history: Iterable[dict[str, Any]] = ()) -> EvidencePacket:
    """Assemble the packet for one work order — from its PULL REQUEST when it has one.

    The three cases are spec §3's table, and `packet.source` records which one happened.
    A pull request that cannot be read falls back to the worktree AND says so in
    `pr_error`: the panel must be able to tell "the submitter pointed at something
    unreadable" from "the submitter changed nothing".

    Never raises — not for a repository that is missing, empty, broken or gone, and not
    for a GitHub that is down. A collector that throws would turn "the evidence is thin"
    into "the round crashed", and the empty packet (`files == ()` and no side effects) is
    what the round machine escalates on.

    **When the worktree is gone the packet is empty, and git is NOT run anywhere else.**
    Falling back to the project root would diff the user's own checkout — whatever they
    happen to have open — and present it to the panel as this worker's evidence. That is
    a silent lie, and it is the one thing this function must never do.

    `wo` is a `work_orders` row. Note what is NOT read from it: `branch`. That column is
    declared and written by nothing in the codebase, so it is always NULL; the base comes
    from git, via the pinned ladder, or from the pull request's own refs.

    `spec` is `specs.spec_of`'s result, `side_effects` is `ops.side_effects_of`'s,
    `assumptions` is `ProjectStore.all_assumptions`'s and `history` is
    `ops.prior_round_history`'s — all passed in rather than looked up because this module
    reads a repository and never a database, the same separation that keeps `ProjectRef`
    a two-line stand-in instead of a `ProjectSpec` import.

    **`history` is empty on the round-opening call and filled on the judging one.**
    `ops.submit_for_validation` builds a packet only to fingerprint it, and `history` is
    excluded from that hash, so the two packets differing costs nothing (spec §5.1).
    """
    pr_url = str(wo.get("pr_url") or "")
    pr_data: dict[str, Any] | None = None
    pr_error = source = ""
    base = head = stat = diff = ""
    files: tuple[str, ...] = ()

    if pr_url:
        pr_data, pr_diff, pr_error = _pull_request(pr_url, project_path)
        if pr_data is not None:
            source = "pull_request"
            base, head = str(pr_data["base_ref"]), str(pr_data["head_ref"])
            stat, diff = str(pr_data["stat"]), pr_diff
            files = _dedupe(pr_data["files"])

    if source != "pull_request":
        source = "worktree"
        # type: ignore — `ProjectRef` carries the one attribute that helper reads.
        worktree = worker_session.worktree_path(ProjectRef(project_path), wo)  # type: ignore[arg-type]
        if worktree is not None:
            base = base_ref(worktree)
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

    effects = tuple(dict(e) for e in side_effects)
    # BEFORE `_truncate`, and that ordering is the whole correctness of the map — see
    # `file_digests`.
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
    )


def _pull_request(url: str,
                  project_path: Path) -> tuple[dict[str, Any] | None, str, str]:
    """`(artifact-as-a-dict, diff, "")`, or `(None, "", why-not)`. Never raises.

    **THE DIFF IS RETURNED SEPARATELY, and the dict it is absent from is the one that
    becomes `packet.pr`.** The rule that a packet never carries the untruncated diff in
    any field (kn-c8b9c7da, where a `full_diff` field was rejected for exactly this) is
    enforced HERE, by the diff never being in that dict at all — not by a caller
    remembering to remove it. A caller that forgets a `.pop` leaks silently: the limit
    still reads as configured and every seat prompt is simply enormous. There is nothing
    to forget now. Rejected in review, round 2.

    The lazy import is the house style AND the thing the leaf rule turns on: `github`
    can only ask GitHub questions — every command it runs is in `github.READ_ONLY_VERBS`
    — so importing it cannot give this module, or anything downstream of it, a way to
    talk back to the submitter it is gathering evidence about (spec §2).

    **`pr_error` IS `GitHubError.reason` AND NEVER `str(e)`.** The exception text
    carries `gh`'s stderr, and this string is rendered verbatim into five seat prompts;
    `reason` is a short phrase `github.py` wrote itself, from a fixed vocabulary. A
    judge's prompt is not a place to interpolate a string a remote server chose.

    **BOTH branches log.** `github._run` logs the expected failures where it raises
    them; the bare `except` below logs its own, because that branch is for the failure
    nobody predicted and a silent fallback to the worktree would be undiagnosable — the
    packet would say only "could not be read" and nothing anywhere would say why.
    """
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
        # `head_sha` in the packet and on `validation_rounds`, `head_oid` on the
        # GitHub dataclasses that mirror `headRefOid`. One fact, named for GitHub on
        # the way in and for the OS once recorded; this line is the whole boundary.
        "head_ref": art.head_ref, "head_sha": art.head_oid,
        "additions": art.additions,
        "deletions": art.deletions, "files": list(art.files), "stat": art.stat,
        "checks": [dict(c) for c in art.checks],
    }, art.diff, ""


def _spec_ref(spec: dict[str, str] | None) -> str:
    """`<path> § <section>`, or "" when there is no resolved section to cite."""
    if not spec or not spec.get("section_text"):
        return ""
    return f"{spec.get('repo_path', '')} § {spec.get('section', '')}".strip()


def collect_feature(project_path: Path, fo: dict[str, Any], children: list[dict[str, Any]],
                    *, declared: str, summary: str = "",
                    diff_chars: int = DEFAULT_DIFF_CHARS,
                    side_effects: Iterable[dict[str, Any]] = (),
                    history: Iterable[dict[str, Any]] = ()) -> EvidencePacket:
    """Assemble the packet for one feature order, from the PROJECT ROOT.

    Every child passed its own review on its own diff, so the marginal defect a
    feature-level panel can find is an INTEGRATION defect: two children each correct
    alone and wrong together. That is only visible in one place — the default branch,
    where the children's work has actually met — so this collector reads the project
    checkout and never a worktree.

    **The range is `base_sha...<default branch head>` and there is no working-tree half.**
    A work order's packet adds `git diff HEAD` because a worker that forgot to commit has
    still produced the change; the project root has no such excuse. Whatever is
    uncommitted there belongs to the user's own session, and shipping it to a panel as
    the feature's evidence would be the same silent lie `collect_work_order` refuses to
    tell when a worktree is missing.

    **A NULL `base_sha` yields an EMPTY packet, and no git command is run.** Feature
    orders that predate the column have none, and the alternatives are all guesses: the
    project's first commit, the oldest child's branch point, `HEAD~n`. A guessed base
    produces a diff that is confidently wrong — the wrong files, plausibly sized, with
    nothing on its face to say so — which is strictly worse for a reviewer than no diff
    at all. The round machine escalates on `files == ()` and a human looks.

    `children` are the feature's child work-order rows. Each contributes its title, its
    `result_summary` and, under the key `declared`, what its own last validation round
    was told — assembled by the caller, because this module may not read a store.

    **`summary` is a parameter here and a column there**, and that asymmetry is the point:
    a work order's `--summary` is written to `work_orders.result_summary` before its
    packet is collected, so `collect_work_order` reads the row. A feature order has no
    such column — the manager's `--summary` lives on the round it opened — so a collector
    that only read `fo` would drop it, and the seats would judge a submission whose author
    said nothing about it. That is the silent-data-loss shape the design doc's 2026-08-22
    correction names, arriving exactly where it said it would.
    """
    base = str(fo.get("base_sha") or "")
    head = default_branch_head(project_path) if base else ""
    stat = diff = ""
    files: tuple[str, ...] = ()
    if base and head:
        rng = f"{base}...{head}"
        stat = _git(project_path, *_diff_args(rng, "--stat"))
        diff = _git(project_path, *_diff_args(rng))
        files = _dedupe(_git(project_path, *_diff_args(rng, "--name-only")).split("\n"))

    effects = tuple(dict(e) for e in side_effects)
    kept, truncated, dropped = _truncate(diff, diff_chars, files)
    return EvidencePacket(
        unit="feature",
        subject_id=str(fo.get("id") or ""),
        title=str(fo.get("title") or ""),
        description=str(fo.get("description") or ""),
        summary=summary,
        declared=declared,
        # A feature order never has a pull request of its own: its children each opened
        # one and the user merged them, which is precisely what made this diff exist.
        pr_url="",
        base=base,
        head=head,
        stat=stat,
        files=files,
        diff=kept,
        diff_truncated=truncated,
        dropped_files=dropped,
        diff_sha=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        # `source` stays "worktree": a feature's diff is read from the project root and
        # a feature order has no pull request of its own, so there is no third value to
        # invent. The one thing a seat must not be told is that this came from a PR.
        side_effects=effects,
        side_effects_sha=side_effects_digest(effects),
        children=tuple(
            {"id": str(c.get("id") or ""), "title": str(c.get("title") or ""),
             "summary": str(c.get("result_summary") or ""),
             "declared": str(c.get("declared") or "")}
            for c in children),
        history=_history(history),
    )


def default_branch_head(repo: Path) -> str:
    """The sha the default branch points at right now, or "" if there is no answer.

    The SAME pinned ladder `collect_work_order` resolves a merge base with
    (`base_ref`), resolved one step further to a sha. Two uses, one ladder: a
    feature whose `base_sha` was recorded against `origin/main` and whose head was later
    read off `main` would diff two different branches and blame the difference on the
    feature.

    "" when the ladder finds nothing, and the caller must treat that as "no diff" rather
    than falling back to `HEAD`. On a checkout with no default branch, `HEAD` is whatever
    the user last checked out — which is exactly the confidently-wrong base this module
    refuses to invent.
    """
    ref = base_ref(repo)
    return _git(repo, "rev-parse", ref).strip() if ref else ""


def base_ref(worktree: Path) -> str:
    """The pinned merge-base ladder. "" means rung 4: diff the working tree against HEAD.

    Rung 1 is the repository's own answer, which is why it comes first: `origin/HEAD` is
    what the remote says its default branch is, so a project on `master`, `trunk` or
    anything else is right without configuring Jarvis. The two guesses below it exist
    for the common case of a repo cloned without `--single-branch`, or one with no
    remote at all.

    PUBLIC because `landing` asks the same question and must not answer it differently —
    "which branch is the default" is the module docstring's example of a question that
    gets guessed per caller the moment each caller owns a copy. It is the THIRD use of
    one ladder now, and rung 4 means something different to each: a diff against HEAD
    here, and "ahead of the default branch has no meaning" there.
    """
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
    """The `.path` that `worker_session.worktree_path` reads, and nothing else.

    That helper is typed for a `catalog.ProjectSpec`, but it touches exactly one
    attribute, and importing the catalog here would drag config loading into a module
    whose whole value is that it depends on nothing. Collectors take a plain path.

    PUBLIC because `ops.unlanded_work` needs the same stand-in and a second copy of a
    one-attribute shim is how two callers end up disagreeing about where a worktree
    lives.
    """

    path: Path


# --------------------------------------------------------------------------- internals


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
