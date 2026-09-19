"""Writing to the tracker: the second half of a bug the OS filed itself.

`bugreport` creates a GitHub issue and stops there, which made the OS's own tracker a
thing only a human could maintain — issue #240. This module is the rest of the
lifecycle: the issue is LABELLED when a work order picks it up, and CLOSED, with a
comment naming the work order and the pull request, when that work lands.

## Why this is not in `github.py`

`github.py`'s whole claim is that it cannot write, and a test walks its AST to prove it
(the validation panel's blind review rests on that — see its module docstring). So every
`gh` WRITE the OS performs lives here instead, and `ISSUE_VERBS` below names each one,
held to the same AST test in `tests/test_issue_lifecycle.py`. "What can Jarvis write to
GitHub" therefore has exactly one answer, in one file, in a list.

## Which repositories the OS may write to

TWO, and the second arrived with the validation panel's follow-ups (user ruling,
2026-09-16). The bug-tracker lifecycle above writes to `bugreport.bug_repo()`. A panel
follow-up is filed on **the repository of the project under review** — the finding is
about that project's code and belongs on that project's tracker, not on the OS's.

**The rule that replaced "one constant repository" is not weaker, and the distinction is
where the repository NAME comes from.** It is never a string out of the record and never
anything a model wrote: it is `github.origin_repo(<the project's checkout>)`, a local
`git remote get-url origin` already declared in `github.LOCAL_GIT_READS`. `checked_repo`
then holds that name to an anchored shape before it can be an argument, and
`checked_issue_url` holds every URL to an anchored shape AND to the repository the
CALLER named — so a URL out of the record still cannot redirect a write, which is the
whole of what `github.UntrustedPullRequest` spells out. What changed is who names the
repository, not whether one is checked.

## What may be published on one

A second question, and it is not the same as which repository: `repo_is_private` is the
allowlist a validation follow-up's TEXT is held to. No prose a model wrote goes to a
tracker the OS could not establish is private — the rule §8 of
docs/superpowers/specs/2026-09-14-a-filed-bug-runs-itself.md states for the OS's own
tracker, applied to the project's (spec §9 of
docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md).

## Desired state, not a sequence of pokes

The OS never fires "label it now" at GitHub and hopes. `desired_state` turns a work
order's status into one of three tracker states, `apply` moves the issue there, and the
`issue_state` column records what was last applied — so the daemon's sweep is a
comparison, costs nothing while the two agree, and RETRIES for free when `gh` is
unreachable. That is what makes E (fail closed, never half-apply) and D (idempotency,
reopening, a human who closed it first) fall out of the design rather than out of a
guard per call site.

The OS never fights a human. An issue a person reopened or closed by hand is left where
they put it — `apply` re-reads the issue before it writes, and records what it found.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .bugreport import bug_repo, gh_bin, gh_missing_message
from .github import GhUnavailable, GitHubError

log = logging.getLogger("jarvis.issues")

#: Every `gh` command this module builds, as the (verb, subverb) pair that opens the
#: argument list. ALL OF THESE ARE WRITES except `issue view`, which is the read that
#: makes the writes idempotent. `tests/test_issue_lifecycle.py` walks this module's AST
#: and holds every argument list against this tuple — the same mechanism that keeps
#: `github.py` read-only, pointed the other way: a command added here has to be added
#: deliberately, in a commit that also edits the test.
ISSUE_VERBS = (("issue", "view"), ("issue", "edit"), ("issue", "comment"),
               ("issue", "close"), ("label", "create"),
               # Added deliberately, in the commit that also edited the AST test — which
               # is the mechanism working, not a hole in it. `issue create` files a
               # validation follow-up; `issue list` is the READ that dedupes it, and it
               # is here rather than in `github.py` because that module may build no
               # command against a repository it was not given by `origin`.
               ("issue", "create"), ("issue", "list"),
               # A READ, in a list that is otherwise writes — same shape as
               # `("issue", "list")` above. It is here rather than in `github.py` for
               # that module's own reason: it may build no command against a repository
               # it was not given by `origin`. `repo_is_private` is what decides whether
               # a seat's words may be published at all.
               ("repo", "view"))

#: One round trip against GitHub's API. Same budget as `github.GH_TIMEOUT`, and for the
#: same reason: the daemon's reconcile tick must not block on a network problem.
GH_TIMEOUT = 30

#: The ONLY shape an issue URL may have before it becomes an argument — anchored at both
#: ends, `https://` required, which is also what makes a leading `-` unreachable so no
#: argument can be read as a `gh` flag. `github.PR_URL_RE`'s twin.
ISSUE_URL_RE = re.compile(
    r"^https://([A-Za-z0-9.-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/issues/([0-9]+)$")

#: The three tracker states a work order can want its issue to be in. `RELEASED` is
#: pointedly not `IN_PROGRESS` minus a label: it is "open, and nobody is working on it",
#: which is where a cancelled or failed work order must leave the tracker (issue #240 C).
IN_PROGRESS = "in-progress"
RELEASED = "released"
CLOSED = "closed"

#: What an untracked work order reports, so callers never have to special-case None.
UNTRACKED = ""

#: The colour and blurb a freshly created label gets. GitHub refuses `--add-label` for a
#: label the repository does not have, and the fleet's tracker is not guaranteed to carry
#: whatever name the catalog names, so the label is ensured before the first add.
LABEL_COLOUR = "0e8a16"
LABEL_DESCRIPTION = "A Jarvis work order is on this."


# -- priority: the only thing that routes a filed bug --------------------------------
#
# The user's ruling, 2026-09-14, settling issue #240's decision A. Every bug carries one
# of five levels; `critical` and `blocker` get a work order and a release once it lands,
# and the other three are queued in the backlog for the user to promote when they choose.
# Nothing else routes — no per-project switch, no inference, no default.

#: Lowest to highest. Order matters: `downgrade_to` compares positions.
PRIORITIES = ("low", "medium", "high", "critical", "blocker")

#: The two that spend money. They are the ONLY ones Neo re-assesses, because they are the
#: only ones that commit the fleet to anything — the other three commit it to a row in a
#: list the user reads at their leisure.
DISPATCHING_PRIORITIES = ("critical", "blocker")

#: The highest level that commits the fleet to nothing. Where a downgrade lands when Neo
#: did not name a level: still a refusal to dispatch, which is the safe direction.
SAFE_DOWNGRADE = "high"


def priority_label(level: str) -> str:
    """The tracker's own copy of the priority, so it shows without opening the OS."""
    return f"priority: {checked_priority(level)}"


#: Every priority label, for the sweep that has to take the old one off before putting the
#: new one on. Derived, so a sixth level can never be half-implemented.
PRIORITY_LABELS = tuple(f"priority: {p}" for p in PRIORITIES)

#: WHAT THE LEVELS MEAN, written once and shown wherever a level is asked for or judged:
#: the `jarvis bug report` interface, the error when the flag is missing, and the prompt
#: Neo re-assesses against. One text, because a rubric that exists in two wordings is two
#: rubrics — and the whole point is that the levels mean the same thing to every agent in
#: the fleet.
#:
#: THE TOP TWO ARE DEFINED BY EFFECT ON THE FLEET, never by how much the bug annoyed
#: whoever hit it. That is the user's instruction and it is also the only definition that
#: survives self-rating: an agent that has just lost an hour to something will call it
#: critical, and "did this stop work, lose state, or make the OS lie" is a question it
#: cannot answer in its own favour.
PRIORITY_RUBRIC = """\
blocker  — the fleet cannot work. Work orders cannot be dispatched, the daemon is down
           or wedged, or every worker in a project is stuck behind this. Nobody can make
           progress until it is fixed.
critical — work is LOST or the OS LIES. State, messages, assumptions or work-order
           history are destroyed or silently dropped; or a surface reports something
           untrue (a status, a cost, a landing, an attention flag) so the user acts on a
           false picture. A single work order dying is not this; a work order dying
           WITHOUT SAYING SO is.
high     — a real defect with a workaround. It costs time or a retry every time it is
           hit, but the work gets done and the record stays true.
medium   — a defect that is annoying or wasteful but bounded: one surface, one command,
           one avoidable path.
low      — cosmetic, a rough edge, a wording problem, or a suggestion.

`critical` and `blocker` commit the fleet to a fix and a release, so they are
RE-ASSESSED BY NEO against this rubric before anything acts on them. Rate the effect on
the fleet, not the inconvenience to you."""


def checked_priority(level: str) -> str:
    """`level` if it is one of the five. Raises `ValueError` otherwise.

    A plain `ValueError` rather than an `IssueLifecycleError`: the callers that validate
    a priority are the CLI and `report_bug`, neither of which is talking to GitHub, and
    each wraps it in the error type its own audience reads.
    """
    got = (level or "").strip().lower()
    if got not in PRIORITIES:
        raise ValueError(
            f"{level!r} is not a priority — pick one of {', '.join(PRIORITIES)}.\n\n"
            f"{PRIORITY_RUBRIC}")
    return got


def dispatches(level: str) -> bool:
    """Does this level commit the fleet to a fix and a release?"""
    return (level or "").strip().lower() in DISPATCHING_PRIORITIES


def downgrade_to(claimed: str, stated: str) -> str:
    """Where a Neo downgrade lands, given what (if anything) Neo named.

    FAILS TOWARDS NOT DISPATCHING, in both of the two ways this can go wrong. A level Neo
    did not state, or stated unintelligibly, becomes `SAFE_DOWNGRADE`; and a "downgrade"
    that names a level at or above the claim is not a downgrade at all, so it becomes
    `SAFE_DOWNGRADE` too rather than confirming by the back door. Neo refusing to confirm
    is the whole signal; the exact level it landed on is detail.
    """
    try:
        level = checked_priority(stated)
    except ValueError:
        return SAFE_DOWNGRADE
    if PRIORITIES.index(level) >= PRIORITIES.index(checked_priority(claimed)):
        return SAFE_DOWNGRADE
    return level


class IssueLifecycleError(GitHubError):
    """The tracker could not be moved, and nothing about it was changed.

    A subclass of `GitHubError` on purpose rather than a second exception family: to
    every caller in the OS "GitHub could not be reached or refused" is one case with one
    response (log it, tell the user once, retry next tick), and the `reason` vocabulary
    that case is described by already lives there.
    """


@dataclass(frozen=True)
class Issue:
    """What `gh issue view --json state,labels,number,url` says, and nothing more."""

    url: str
    number: int
    #: GitHub's own enum, uppercased: OPEN or CLOSED.
    state: str
    labels: tuple[str, ...]

    @property
    def closed(self) -> bool:
        return self.state == "CLOSED"

    def has(self, label: str) -> bool:
        """Case-insensitively, because GitHub matches labels that way and a catalog
        that spells the label `In Progress` must not add a second one beside it."""
        low = label.strip().lower()
        return any(l.lower() == low for l in self.labels)


def issue_number(url: str) -> int:
    """The issue's number, or 0 if `url` is not an issue URL at all."""
    match = ISSUE_URL_RE.match(url or "")
    return int(match.group(4)) if match else 0


def checked_issue_url(url: str, repo: str | None = None) -> str:
    """`url` if the OS may write to it. Raises `IssueLifecycleError` otherwise.

    Stricter than `github.checked_pr_url`'s origin test, and deliberately so: that one
    skips its repository check when `origin` cannot be read, because losing PR polling
    over a moved checkout would be worse than the exposure. There is no degraded case
    here — the caller always knows which repository it means — so a URL that is not on
    that repository is refused outright.

    `repo` DEFAULTS to the OS's own tracker and is passed explicitly by the follow-up
    path, which writes to the project under review instead (see the module docstring).
    The check is the same either way: the caller names the repository, and a URL out of
    the record cannot disagree with it.
    """
    match = ISSUE_URL_RE.match(url or "")
    if match is None:
        raise IssueLifecycleError(
            f"{url!r} is not an issue URL this OS will write to",
            GitHubError.URL_REFUSED)
    want = (repo or bug_repo()).lower()
    got = f"{match.group(2)}/{match.group(3)}".lower()
    if got != want:
        raise IssueLifecycleError(
            f"{url!r} is on {got}, but this OS only writes to {want}",
            GitHubError.URL_REFUSED)
    return url


#: The ONLY shape a label may have before it becomes an argument. Anchored, no leading
#: `-`, and a character set that cannot produce one — the same rule as `ISSUE_URL_RE` and
#: for the same reason (review round 1): the label is the OTHER argv value this module
#: interpolates, it comes from a catalog key that needs no `--reason` to change, and
#: `gh` would read a leading dash as a flag. 50 is GitHub's own ceiling.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$")


#: The ONLY shape a repository may have before it becomes a `--repo` argument. Anchored
#: and dash-free at the front for `LABEL_RE`'s reason: `gh` reads a leading `-` as a flag,
#: and this value now varies per project instead of being one constant.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def checked_repo(repo: str) -> str:
    """`repo` if it may become a `gh --repo` argument. Raises otherwise.

    Its callers derive the name from `github.origin_repo`, which reads the checkout's own
    `origin`. That is already trustworthy; this is the layer that stays true when some
    later caller derives it from somewhere else.
    """
    if not REPO_RE.match(repo or ""):
        raise IssueLifecycleError(
            f"{repo!r} is not a repository this OS will send to GitHub — it must be "
            f"`owner/name`, starting with a letter or digit",
            GitHubError.URL_REFUSED)
    return repo


def checked_label(label: str) -> str:
    """`label` if it may become a `gh` argument. Raises `IssueLifecycleError` otherwise.

    Also enforced at catalog-parse time (`catalog._parse_bugs`), which is where the
    message can name the key. This is the layer that cannot be skipped: a project row
    edited by hand reaches `gh` through here whatever the parser saw.
    """
    if not LABEL_RE.match(label or ""):
        raise IssueLifecycleError(
            f"{label!r} is not a label this OS will send to GitHub — it must start with "
            f"a letter or digit and use only letters, digits, spaces and ._:/-",
            GitHubError.URL_REFUSED)
    return label


def _run(args: list[str], *, url: str, tolerate: str = "",
         stdin: str | None = None) -> str:
    """One `gh` call. Raises `IssueLifecycleError` unless `tolerate` matches its stderr.

    `tolerate` is the one concession to `gh`'s habit of failing on a no-op: creating a
    label that already exists is exit 1 with "already exists" on stderr, and that is the
    success case for `ensure_label`.

    `stdin` feeds `--body-file -`. It is a parameter here rather than a second copy of
    this function because a body long enough to need stdin is exactly a body whose call
    needs the same timeout, the same `gh`-missing message and the same rule that remote
    text is logged and never raised.
    """
    shown = "`gh " + " ".join(args[:2]) + f" {url}`"
    try:
        proc = subprocess.run([gh_bin(), *args], input=stdin, capture_output=True,
                              text=True, timeout=GH_TIMEOUT)
    except FileNotFoundError as e:
        raise GhUnavailable(
            gh_missing_message("so Jarvis cannot keep its own tracker up to date"),
            GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise IssueLifecycleError(f"{shown} timed out after {GH_TIMEOUT}s",
                                  GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        if tolerate and tolerate.lower() in detail.lower():
            return ""
        # The remote text is LOGGED, never raised into `reason` — `github.GitHubError`.
        log.info("%s failed: %s", shown, detail)
        raise IssueLifecycleError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def view(url: str) -> Issue:
    """Read the issue at `url`. Raises `IssueLifecycleError` on any doubt."""
    url = checked_issue_url(url)
    stdout = _run(["issue", "view", url, "--json", "number,state,labels,url"], url=url)
    try:
        payload: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise IssueLifecycleError(
            f"`gh issue view {url}` returned no JSON ({stdout[:200]!r})",
            GitHubError.UNREADABLE) from e
    state = str(payload.get("state") or "").upper()
    if not state:
        raise IssueLifecycleError(
            f"`gh issue view {url}` reported no state ({payload!r})",
            GitHubError.UNREADABLE)
    labels = tuple(str(l.get("name") or "") for l in (payload.get("labels") or [])
                   if isinstance(l, dict) and l.get("name"))
    return Issue(url=str(payload.get("url") or url),
                 number=int(payload.get("number") or issue_number(url)),
                 state=state, labels=labels)


def ensure_label(label: str, repo: str | None = None) -> None:
    """Make sure the repository carries `label`, so `--add-label` cannot fail over it.

    Tolerates the already-exists failure rather than asking first: one call in the common
    case instead of two, and `gh label create` is the only thing that can answer the
    question without a race anyway.
    """
    repo = checked_repo(repo or bug_repo())
    _run(["label", "create", checked_label(label), "--repo", repo,
          "--color", LABEL_COLOUR, "--description", LABEL_DESCRIPTION],
         url=repo, tolerate="already exists")


def add_label(url: str, label: str) -> None:
    _run(["issue", "edit", checked_issue_url(url), "--add-label", checked_label(label)],
         url=url)


def remove_label(url: str, label: str) -> None:
    _run(["issue", "edit", checked_issue_url(url), "--remove-label",
          checked_label(label)], url=url)


def comment(url: str, body: str) -> None:
    """Add a comment, with the body on stdin rather than in argv.

    Same reason `bugreport.create_issue` does it: a comment carries a summary and
    several links, and argv has a length limit that stdin does not.
    """
    url = checked_issue_url(url)
    _run(["issue", "comment", url, "--body-file", "-"], url=url, stdin=body)


def close(url: str, body: str) -> None:
    """Close the issue, carrying `body` as the closing comment.

    ONE call, not a comment followed by a close: two calls have a gap in which the
    comment lands and the close does not, and the retry would then post the comment
    again. `gh issue close --comment` is atomic from this side.
    """
    _run(["issue", "close", checked_issue_url(url), "--comment", body], url=url)


# -- what the tracker should say, given what the work order is doing ----------------


# -- validation follow-ups: the panel's non-blocking findings, on the project's tracker --
#
# User ruling, 2026-09-16: a finding the panel judged the work shippable WITHOUT is filed
# as a GitHub issue on the project under review, not as a row in the OS's backlog. Spec
# §4 of docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md, whose §4.2 said
# `CentralStore.add_backlog`; the ruling supersedes it and nothing else in §4 moved.

#: What marks an issue as the panel's rather than a person's. It is BOTH halves of the
#: mechanism: the label the filing applies, and the term the dedupe searches on — so an
#: issue the panel filed is findable without reading every issue on the tracker.
FOLLOW_UP_LABEL = "validation follow-up"

#: The colour and blurb `FOLLOW_UP_LABEL` is created with, distinct from the bug label's
#: green so the two read apart at a glance on a tracker carrying both.
FOLLOW_UP_COLOUR = "c5def5"
FOLLOW_UP_DESCRIPTION = "Raised by the Jarvis validation panel; not a blocker."


def repo_is_private(repo: str) -> bool:
    """True only when GitHub SAID this repository is private; False on any doubt.

    THE ALLOWLIST THE FILING RESTS ON. A follow-up carries a seat's own prose, written by
    a model that does not know it will be published, and §8 of
    docs/superpowers/specs/2026-09-14-a-filed-bug-runs-itself.md forbids publishing that
    to a public tracker. So the text goes out only where the OS has POSITIVELY
    established the destination is private, and every other answer — `gh` missing, a
    refusal, a timeout, a repository this installation cannot read, output that will not
    parse — reaches the same branch as "public". "Unknown" is not a third case.

    NOT CACHED. A repository flipped private-to-public between rounds must be seen; one
    round trip per round is the same budget the dedupe read already spends.

    THE REPOSITORY IS POSITIONAL: `gh repo view` has no `--repo` flag (gh 2.86), unlike
    every other command in this module. `checked_repo` is therefore load-bearing twice
    over — it is what stops the name being read as a flag.
    """
    try:
        stdout = _run(["repo", "view", checked_repo(repo), "--json", "isPrivate"],
                      url=repo)
        return json.loads(stdout or "").get("isPrivate") is True
    except Exception as e:  # noqa: BLE001 — every failure has the same safe answer
        log.info("could not establish whether %s is private (%s) — treating it as "
                 "public, so no finding text is published there", repo, e)
        return False


def file_follow_up(repo: str, title: str, body: str) -> str:
    """File one follow-up on `repo` and return its issue URL.

    The body travels on stdin (`--body-file -`) for `bugreport.create_issue`'s reason:
    a finding carries the seat's detail plus a provenance footer, and argv has a length
    limit that stdin does not.

    **THE LABEL IS ENSURED FIRST, and that is not belt-and-braces.** `gh` refuses
    `--label` for a label the repository does not define (`kn-eefc35a8` fact 1), and
    unlike the OS's own tracker these repositories have never been asked to carry one —
    the FIRST follow-up on every project in the fleet would fail without this.
    """
    repo = checked_repo(repo)
    ensure_follow_up_label(repo)
    stdout = _run(["issue", "create", "--repo", repo, "--title", title,
                   "--label", FOLLOW_UP_LABEL, "--body-file", "-"],
                  url=repo, stdin=body)
    url = (stdout or "").strip().splitlines()[-1] if (stdout or "").strip() else ""
    # Held to the shape AND to the repository we just named, exactly as every other URL
    # in this module is: the answer came back over a pipe, and it becomes an argument.
    return checked_issue_url(url, repo)


def ensure_follow_up_label(repo: str) -> None:
    """`ensure_label` with the follow-up's own colour and blurb.

    Not `ensure_label(FOLLOW_UP_LABEL, repo)`: that one describes a label meaning "a work
    order is on this", which is the opposite of what a follow-up is — nobody is on it.
    """
    repo = checked_repo(repo)
    _run(["label", "create", checked_label(FOLLOW_UP_LABEL), "--repo", repo,
          "--color", FOLLOW_UP_COLOUR, "--description", FOLLOW_UP_DESCRIPTION],
         url=repo, tolerate="already exists")


def follow_ups_filed(repo: str, unit_id: str) -> list[dict[str, Any]]:
    """Every follow-up the panel has already filed on `repo` for one unit.

    The dedupe's source of truth, and it is the TRACKER rather than any local record for
    the reason the backlog was before the ruling: "has this already been filed" is a
    question about the destination.

    **`--state all`, and that keyword is the whole correctness of this.** `gh issue list`
    defaults to OPEN issues only. A follow-up the user has since closed would drop out of
    the answer, the dedupe would stop seeing it, and every subsequent round would file it
    again — for ever. It is the exact trap `CentralStore.list_backlog`'s `status="open"`
    default set on the path this replaced, one layer out.

    Searched by LABEL AND by the unit id in the body, so the query is one round trip per
    round rather than one per finding, and it cannot match a person's own issue that
    happens to share a title. An unreachable `gh` raises; the caller decides what a round
    that could not read the tracker does.
    """
    repo = checked_repo(repo)
    stdout = _run(["issue", "list", "--repo", repo, "--state", "all",
                   "--label", checked_label(FOLLOW_UP_LABEL),
                   "--search", f"{unit_id} in:body", "--limit", "100",
                   "--json", "number,title,url,state"], url=repo)
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("url")]


def desired_state(store: Any, wo: dict[str, Any]) -> str:
    """Where this work order says its issue belongs. The whole policy, in one function.

    Three answers and every one of them is DERIVED FROM THE RECORD, never asserted when
    something happens — the rule kn-b437a0e5 states for attention and which holds for the
    same reason here: a tracker state written by one event and never re-derived goes on
    being true after the thing that made it true has stopped.

    * **`IN_PROGRESS`** — the work order is open and nobody has refused its work.
    * **`RELEASED`** (open, unlabelled) — three cases in one predicate. The order failed
      or was cancelled; or its pull request was closed unmerged, which `pr_closure_told`
      answers and which REVERSES ITSELF when the pull request is reopened; or the user
      closed it by hand over code that never landed, which is `work_unlanded_open`. In
      all three nothing is under way, so the label would be a lie — issue #240 C.
    * **`CLOSED`** — the order completed with nothing left stranded. Two routes reach it
      and they are not the same claim: a merged pull request (`ops.complete_merged` — the
      landing signal, the answer issue #232 says `completed` alone cannot give), and a
      `jarvis wo done` over an order that produced no code to land at all. The second is
      Neo's ruling on question 289, off the asymmetry `ops.mark_done` already embodies:
      it RECORDS unlanded work rather than refusing it, so the same `work_unlanded_open`
      that keeps a stranded order out of `CLOSED` lets a not-a-bug or a docs-only one in.
    """
    from .project_store import OPEN_STATUSES

    wo_id = wo["id"]
    if wo["status"] in OPEN_STATUSES:
        return RELEASED if store.pr_closure_told(wo_id) else IN_PROGRESS
    if wo["status"] == "completed" and not store.work_unlanded_open(wo_id):
        return CLOSED
    return RELEASED


def closing_comment(wo: dict[str, Any]) -> str:
    """What the issue says about its own closure, months later.

    The id, the title and the pull request, and NOTHING ELSE. That list is the whole
    decision (issue #240 F): whoever reads this issue next must be able to get from it to
    the work order and to the code, and everything beyond that is data leaving the
    machine.

    **`result_summary` IS DELIBERATELY NOT HERE** (review round 1). It is worker prose
    written for the internal record by a worker that does not know it will be published,
    and it routinely carries absolute paths, internal project names and raw error text.
    Publishing it to a PUBLIC tracker with nobody reading it first is not something the
    user agreed to: the consent `bugs` collects is described everywhere — the config
    docstring, the CLAUDE.md crib, the `report-jarvis-bug` skill — as closing the issue
    "with the work order and the PR on it". Widening what is published is a change to
    what that sentence means, not an implementation detail, and it would apply
    retroactively to every tracked order the moment it shipped.

    Anyone who wants the detail follows the work-order id, which is exactly what it is
    here for.
    """
    parts = [f"Closed by Jarvis work order `{wo['id']}` — {wo.get('title') or ''}".strip()
             + "."]
    if wo.get("pr_url"):
        parts += ["", f"Landed in {wo['pr_url']}."]
    else:
        parts += ["", "Closed with no pull request: this work order produced no code to "
                      "land."]
    parts += ["", "<!-- Closed automatically by the Jarvis bug lifecycle (issue #240). -->"]
    return "\n".join(parts)


def apply(store: Any, wo: dict[str, Any], label: str) -> str:
    """Move this work order's issue to where `desired_state` says it belongs.

    Returns the state the tracker is now in, for the caller to record in `issue_state`.
    Raises `IssueLifecycleError` if anything could not be done, having done as little as
    possible — the caller records nothing and the next sweep tries again.

    **IT NEVER FIGHTS A HUMAN.** The issue is re-read first, and an issue a person has
    already closed is left closed whatever the work order is doing: the OS does not
    reopen, and it records what it found so it stops asking. Reopening is therefore a
    person's prerogative in both directions — reopen a closed issue and the sweep leaves
    it alone too, because the work order that closed it is settled and its desired state
    has not changed.

    **THE ORDER OF THE TWO WRITES IN THE CLOSING CASE IS LOAD-BEARING.** The label comes
    off BEFORE the close, so a close that fails is retried against an issue that is still
    open and gets its comment exactly once. Closing first and failing on the label would
    leave the next sweep facing an already-closed issue with `issue_state` unwritten,
    which is the one shape that posts a duplicate comment.
    """
    url = checked_issue_url(wo.get("issue_url") or "")
    want = desired_state(store, wo)
    issue = view(url)

    if want == CLOSED:
        if issue.has(label):
            remove_label(url, label)
        if issue.closed:
            # Someone closed it first. Say what closed it anyway — the record is the
            # point (issue #240 F) — but only on the sweep that discovers it.
            if (wo.get("issue_state") or "") != CLOSED:
                comment(url, closing_comment(wo))
        else:
            close(url, closing_comment(wo))
        return CLOSED

    if issue.closed:
        return CLOSED
    if want == IN_PROGRESS and not issue.has(label):
        ensure_label(label)
        add_label(url, label)
    elif want == RELEASED and issue.has(label):
        remove_label(url, label)
    return want


# -- picking the issue up: the step between "filed" and "being worked on" -----------


def tracker_project(catalog: Any, repo: str | None = None) -> Any:
    """The project that would FIX a bug on `repo`, or None if none would.

    ONE condition, and it is the project's own: its git `origin` IS that repository. The
    project that NOTICED the bug has no say — `jarvis bug report` runs wherever the
    symptom appeared, and that project pays for nothing.

    There is deliberately no opt-in switch beside it any more. Routing is by priority and
    only by priority (the user's ruling of 2026-09-14): what stops a filing committing the
    fleet to work is the rubric plus Neo's re-assessment, not a catalog key — and a key
    that shipped off would have made the ruling's own "critical and blocker automatically
    get a work order" unreachable.
    """
    from . import github

    want = (repo or bug_repo()).lower()
    for spec in getattr(catalog, "projects", []):
        origin = github.origin_repo(spec.path)
        if origin and f"{origin[0]}/{origin[1]}" == want:
            return spec
    return None


#: What the worker is told to do, above the issue body. The worker sees ONLY the
#: description, so the issue URL has to be in it — `jarvis bug report` is the only thing
#: that knows the two belong together.
WORK_ORDER_BRIEF = """\
Fix {url}.

This bug is `{priority}`, confirmed by Neo against the OS's own rubric, which is why it \
became a work order rather than a backlog item. A release goes out once your fix LANDS.

The issue text is reproduced below; it is what the reporting agent saw. Read the issue \
itself first if anything since then has been added to it.

When this work lands, Jarvis closes the issue itself — do not close it by hand, and do \
not open a second issue for the same thing.

---

{body}"""

#: The backlog item a filed bug always becomes first. The issue URL is in the text rather
#: than in a column because the backlog is an OS-wide intake list that knows nothing about
#: trackers, and a column there would be one more thing for every other intake path to
#: leave NULL.
BACKLOG_NOTE = """\
Filed through `jarvis bug report` as `{priority}`.

{url}

---

{body}"""


def route_filing(issue_url: str, title: str, body: str, priority: str,
                 repo: str | None = None) -> dict[str, Any]:
    """Everything that happens to a freshly filed bug, before anyone is told about it.

    Returns what ACTUALLY happened, always — it never raises, because by the time it runs
    the issue exists and an exception here would report "not filed" about a bug that was
    (`bugreport.report_bug`'s rule). Every outcome carries a `reason` the user reads.

    **THE BACKLOG IS WHERE EVERY BUG LANDS**, whatever its priority, and that is what
    makes failing closed the default rather than a branch. A `critical` or `blocker`
    filing is a CLAIM: it goes to the backlog like the rest and a `triage` question goes
    to Neo, and only Neo confirming it promotes the item out. So a Neo that is off,
    unreachable or unintelligible leaves the bug exactly where it is — queued, visible,
    and marked unconfirmed — which is the direction the user asked for. There is no path
    from a filing to a work order that does not go through a verdict.

    `low`, `medium` and `high` are not re-assessed. They commit the fleet to nothing, so
    there is nothing to guard against.
    """
    from .catalog import CatalogError
    from .central_store import CentralStore
    from .ops import OpsError, registered_project_paths, resolve_catalog

    out: dict[str, Any] = {"priority": priority, "wo_id": "", "backlog_id": "",
                           "project": "", "neo_question_id": 0, "reason": ""}
    try:
        spec = tracker_project(resolve_catalog(), repo)
    except (CatalogError, OpsError, FileNotFoundError, OSError) as e:
        out["reason"] = f"the fleet catalog could not be read ({e})"
        return out
    if spec is None:
        out["reason"] = (f"no registered project has {repo or bug_repo()} as its git "
                         f"origin, so the issue was filed and left for a human")
        return out
    out["project"] = spec.name
    if spec.name not in registered_project_paths():
        out["reason"] = (f"project {spec.name!r} is in the catalog but not registered — "
                         f"run `jarvis start` before bugs can be picked up")
        return out

    # The tracker's own copy of the level, first: it is the one piece of this that is
    # useful even if everything below fails, because it is what the issue shows to
    # somebody who never opens the OS.
    try:
        set_priority_label(issue_url, priority)
    except GitHubError as e:
        out["label_error"] = str(e)

    central = CentralStore()
    try:
        item = central.add_backlog(
            spec.name, title,
            description=BACKLOG_NOTE.format(priority=priority, url=issue_url, body=body),
            origin_note=f"jarvis bug report ({priority})")
    finally:
        central.close()
    out["backlog_id"] = item["id"]

    if not dispatches(priority):
        out["reason"] = (f"`{priority}` is queued for the user to promote: "
                         f"`jarvis backlog promote {item['id']}`")
        return out

    try:
        out["neo_question_id"] = ask_triage(spec.name, issue_url, title, body, priority,
                                            item["id"])
        out["reason"] = (f"`{priority}` claimed — Neo is re-assessing it against the "
                         f"rubric; a work order is created only if Neo confirms")
    except Exception as e:  # noqa: BLE001 — the issue and the backlog item both exist
        out["reason"] = (f"`{priority}` claimed but NOT confirmed — Neo could not be "
                         f"asked ({e}). It is queued at {item['id']} and nothing was "
                         f"dispatched.")
    return out


def set_priority_label(issue_url: str, level: str) -> None:
    """Make the issue carry exactly one priority label: this one.

    Every other level comes OFF, which is what makes this usable for a re-assessment as
    well as for a filing — Neo downgrading a claim must not leave the tracker showing
    both. Reads the issue first, so a re-run costs one call and writes nothing.
    """
    want = priority_label(level)
    issue = view(issue_url)
    for stale in PRIORITY_LABELS:
        if stale != want and issue.has(stale):
            remove_label(issue_url, stale)
    if not issue.has(want):
        ensure_label(want)
        add_label(issue_url, want)


#: The question Neo re-assesses a `critical`/`blocker` claim with. The rubric travels WITH
#: it rather than being left to Neo's learnings: this is the text the claim is judged
#: against, so it has to be the same text the filing agent was shown (`PRIORITY_RUBRIC`).
TRIAGE_QUESTION = """\
A fleet agent filed a Jarvis OS bug and rated it `{priority}`. That rating is a CLAIM, \
not a verdict, and it is the only thing standing between this report and a work order \
plus a release — so you are re-assessing it before anything acts on it.

Re-assess it against this rubric and the report's own evidence, and answer on the effect \
on the FLEET rather than on how much the bug inconvenienced whoever hit it. Agents \
filing bugs systematically over-rate their own: the question is not whether this is worth \
fixing, it is whether it stops work, loses state, or makes the OS tell the user something \
untrue.

{rubric}

Reply with the ordinary verdict JSON. `verdict: approve` CONFIRMS `{priority}` and the \
OS creates the work order and ships the fix once it lands. `verdict: deny` DOWNGRADES \
it — put the level you think it is as the first word of `answer` (one of {levels}), and \
your reasoning in `reason`; the bug then waits in the backlog like any other. Escalate \
only if the report does not give you enough to judge either way.

Issue: {url}
Title: {title}

{body}"""


def ask_triage(project: str, issue_url: str, title: str, body: str, priority: str,
               backlog_id: str) -> int:
    """Queue the re-assessment. Returns the Neo question id.

    The question row IS the pending-triage record — there is no second table and no
    `pending` column anywhere. Everything the daemon needs to act on the verdict travels
    in `context` as JSON, which means a triage that is never answered leaves no state to
    clean up and no row that could disagree with the backlog.
    """
    from .neo_store import NeoStore

    context = json.dumps({"issue_url": issue_url, "title": title,
                          "priority": priority, "backlog_id": backlog_id,
                          "body": body[:4000]})
    neo = NeoStore()
    try:
        q = neo.ask(project, "", TRIAGE_QUESTION.format(
            priority=priority, rubric=PRIORITY_RUBRIC, url=issue_url, title=title,
            levels=", ".join(f"`{p}`" for p in PRIORITIES), body=body[:4000]),
            context=context, kind="triage")
    finally:
        neo.close()
    return int(q["id"])


def triage_payload(q: dict[str, Any]) -> dict[str, Any]:
    """The filing facts back out of a triage question, or `{}` if it is not one.

    Empty rather than raising: the delivery path runs inside the Neo drain, where one
    unreadable row must not stop the queue.
    """
    if q.get("kind") != "triage":
        return {}
    try:
        payload = json.loads(q.get("context") or "")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("issue_url") else {}


#: What the tracker records about the re-assessment. BOTH FACTS, ALWAYS — the claim and
#: the verdict — because their disagreement is the signal that says whether the rubric is
#: working, and a comment that only showed the surviving level would delete it.
#: What the TRACKER is told about a re-assessment: the two levels and where the bug went.
#:
#: **NEO'S REASONING IS DELIBERATELY NOT HERE** (review round 2), for the same reason
#: `closing_comment` carries no `result_summary`. It is model prose written for the
#: internal record by a model that does not know it will be published, and Neo answers
#: with fleet context behind it — learnings, other work orders, project names, paths. A
#: GitHub comment is indexed and cached whether or not it is later deleted, and nobody
#: reads this one before it leaves the machine.
#:
#: The reasoning still travels with both levels, on the RECORD the user asked for it on:
#: the Neo question row holds it verbatim (`jarvis neo show <id>`), and the inbox row
#: `daemon._deliver_triage_verdict` writes carries the head of it. Neither is public.
TRIAGE_COMMENT = """\
**Priority re-assessed by Neo:** filed as `{claimed}` → **`{settled}`**.

{tail}

<!-- Jarvis bug lifecycle (issue #240): the filing agent's rating is a claim; Neo
     re-assesses `critical` and `blocker` against the OS's rubric before any work is
     dispatched. The reasoning is on the work-order record, not here. -->"""


def triage_comment(claimed: str, settled: str, tail: str) -> str:
    return TRIAGE_COMMENT.format(claimed=claimed, settled=settled, tail=tail)


def record_applied(store: Any, wo: dict[str, Any], label: str) -> str:
    """`apply`, plus the two writes that stop it being asked again for nothing.

    The column and the event are one unit with the GitHub call: written only after it
    succeeded, which is the whole of "fail closed and never half-apply" (#240 E). Shared
    by the filing path and the daemon sweep so there is one place that decides what
    counts as applied.
    """
    applied = apply(store, wo, label)
    store.update_work_order(wo["id"], issue_state=applied)
    store.add_event(wo["id"], f"issue_{applied.replace('-', '_')}",
                    {"issue_url": wo.get("issue_url"), "label": label})
    return applied


# -- acting on the verdict ------------------------------------------------------------


def settle_triage(catalog: Any, q: dict[str, Any], verdict: dict[str, Any]
                  ) -> dict[str, Any]:
    """Route a bug now that Neo has ruled on its priority. The daemon's whole triage step.

    Three outcomes, and only the first one dispatches anything:

    * **confirmed** (`verdict: approve`) — the work order is created and the backlog item
      is marked promoted. A release follows when the fix LANDS, which is `sync_issues`'
      job and not this one.
    * **downgraded** (`verdict: deny`) — the priority label is corrected and the item
      stays in the backlog for the user to promote when they choose.
    * **unconfirmed** (escalated, or output nobody could parse) — nothing at all. The
      claim stands on the record as a claim, the item waits, and the user is told. The
      refusal direction is the point: an unconfirmed `blocker` that quietly became a
      release is the worse failure.

    The tracker gets a comment carrying BOTH levels in every case where a verdict was
    reached, because the disagreement between the claim and the verdict is what tells the
    user whether the rubric is working. NEO'S REASONING IS NOT ON IT — see
    `TRIAGE_COMMENT`; it rides the internal record instead, where `out["reason"]` carries
    it in full for the caller to put in front of the user.
    """
    from .central_store import CentralStore

    payload = triage_payload(q)
    if not payload:
        return {"outcome": "not-a-triage"}
    claimed = payload.get("priority") or ""
    url = payload["issue_url"]
    out: dict[str, Any] = {"outcome": "", "claimed": claimed, "settled": claimed,
                           "issue_url": url, "wo_id": "",
                           "backlog_id": payload.get("backlog_id") or ""}

    # In full and on every outcome: this is the private half of the record, and the two
    # places it goes (the inbox row, `jarvis neo show`) are the ones that may hold it.
    out["reason"] = verdict.get("reason") or ""

    if verdict.get("escalate") or verdict.get("failed"):
        out["outcome"] = "unconfirmed"
        return out

    if verdict.get("approve"):
        out["outcome"] = "confirmed"
    else:
        out["outcome"] = "downgraded"
        # The FIRST WORD of `answer`, which is what the question asked for. Anything else
        # lands on `SAFE_DOWNGRADE` — see `downgrade_to`; a level nobody stated must never
        # be guessed at upwards.
        stated = ((verdict.get("answer") or "").strip().split() or [""])[0]
        out["settled"] = downgrade_to(claimed, stated.strip("`.,"))

    spec = next((p for p in getattr(catalog, "projects", [])
                 if p.name == q.get("project")), None)
    if spec is None:
        out["outcome"] = "unconfirmed"
        out["reason"] = f"project {q.get('project')!r} is no longer in the catalog"
        return out

    if out["outcome"] == "confirmed":
        out["wo_id"] = promote_confirmed(spec, payload)
        if out["backlog_id"]:
            central = CentralStore()
            try:
                central.mark_backlog(out["backlog_id"], "promoted",
                                     promoted_wo_id=out["wo_id"])
            finally:
                central.close()

    # LAST, and outside the work-order write on purpose: a tracker comment that failed
    # must not be able to undo a promotion that succeeded, and the sweep re-labels anyway.
    try:
        set_priority_label(url, out["settled"])
        comment(url, triage_comment(
            claimed, out["settled"],
            f"A work order is on it: `{out['wo_id']}`." if out["wo_id"] else
            "Queued in the Jarvis backlog; no work order was created."))
    except GitHubError as e:
        out["comment_error"] = str(e)
    return out


def promote_confirmed(spec: Any, payload: dict[str, Any]) -> str:
    """Create the work order a confirmed `critical`/`blocker` earned. Returns its id.

    **NO SECOND WORK ORDER FOR ONE ISSUE** (#240 D). An issue that already has a LIVE work
    order hands that one back; one whose work orders have all settled is free to get
    another, which is exactly what a reopened issue needs.
    """
    from .ops import create_work_order
    from .project_store import TERMINAL_STATUSES, ProjectStore

    url = payload["issue_url"]
    priority = payload.get("priority") or ""
    store = ProjectStore(spec.path)
    try:
        for existing in store.work_orders_for_issue(url):
            if existing["status"] not in TERMINAL_STATUSES:
                return str(existing["id"])
    finally:
        store.close()

    wo = create_work_order(
        spec.name, payload.get("title") or url,
        description=WORK_ORDER_BRIEF.format(url=url, priority=priority,
                                            body=payload.get("body") or ""),
        origin="jarvis", issue_url=url, issue_priority=priority)
    store = ProjectStore(spec.path)
    try:
        # Label it now rather than only on the next sweep: the promotion and the tracker
        # saying so should not be a poll interval apart. A failure is retried there.
        try:
            record_applied(store, store.get_work_order(wo["id"]), spec.bugs.label)
        except GitHubError as e:
            log.info("could not label %s yet: %s", url, e)
    finally:
        store.close()
    return str(wo["id"])
