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

Writes are confined to the OS's own bug tracker (`bugreport.bug_repo()`). A work order's
`issue_url` is a column like `pr_url`, so it is a string the record hands back to a
command run with the operator's credentials; `checked_issue_url` holds it to an anchored
shape AND to that one repository before it can become an argument, for the reasons
`github.UntrustedPullRequest` spells out.

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
               ("issue", "close"), ("label", "create"))

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
    over a moved checkout would be worse than the exposure. Here the repository is a
    CONSTANT (`bugreport.bug_repo()`) — there is no degraded case to be lenient about,
    so a URL that is not on the OS's own tracker is refused outright.
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


def _run(args: list[str], *, url: str, tolerate: str = "") -> str:
    """One `gh` call. Raises `IssueLifecycleError` unless `tolerate` matches its stderr.

    `tolerate` is the one concession to `gh`'s habit of failing on a no-op: creating a
    label that already exists is exit 1 with "already exists" on stderr, and that is the
    success case for `ensure_label`.
    """
    shown = "`gh " + " ".join(args[:2]) + f" {url}`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=GH_TIMEOUT)
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
    _run(["label", "create", label, "--repo", repo or bug_repo(),
          "--color", LABEL_COLOUR, "--description", LABEL_DESCRIPTION],
         url=repo or bug_repo(), tolerate="already exists")


def add_label(url: str, label: str) -> None:
    _run(["issue", "edit", checked_issue_url(url), "--add-label", label], url=url)


def remove_label(url: str, label: str) -> None:
    _run(["issue", "edit", checked_issue_url(url), "--remove-label", label], url=url)


def comment(url: str, body: str) -> None:
    """Add a comment, with the body on stdin rather than in argv.

    Same reason `bugreport.create_issue` does it: a comment carries a summary and
    several links, and argv has a length limit that stdin does not.
    """
    url = checked_issue_url(url)
    shown = f"`gh issue comment {url}`"
    try:
        proc = subprocess.run([gh_bin(), "issue", "comment", url, "--body-file", "-"],
                              input=body, capture_output=True, text=True,
                              timeout=GH_TIMEOUT)
    except FileNotFoundError as e:
        raise GhUnavailable(
            gh_missing_message("so Jarvis cannot keep its own tracker up to date"),
            GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise IssueLifecycleError(f"{shown} timed out after {GH_TIMEOUT}s",
                                  GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("%s failed: %s", shown, detail)
        raise IssueLifecycleError(f"{shown} failed: {detail}", GitHubError.REFUSED)


def close(url: str, body: str) -> None:
    """Close the issue, carrying `body` as the closing comment.

    ONE call, not a comment followed by a close: two calls have a gap in which the
    comment lands and the close does not, and the retry would then post the comment
    again. `gh issue close --comment` is atomic from this side.
    """
    _run(["issue", "close", checked_issue_url(url), "--comment", body], url=url)


# -- what the tracker should say, given what the work order is doing ----------------


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


#: How much of the worker's own summary rides along on the closing comment. Enough for
#: the tracker to say what was done without turning the issue into a second copy of the
#: work-order record, which is what the `wo_id` in the first line is for.
SUMMARY_CHARS = 800


def closing_comment(wo: dict[str, Any]) -> str:
    """What the issue says about its own closure, months later.

    The standard `bugreport.render_body` holds the OPENING comment to, pointed the other
    way: whoever reads this issue next must be able to get from it to the work order and
    to the code without asking anybody. Hence the id, the pull request and the summary —
    and hence the honest sentence when there is no pull request, rather than silence that
    reads like one went missing.
    """
    parts = [f"Closed by Jarvis work order `{wo['id']}` — {wo.get('title') or ''}".strip()
             + "."]
    if wo.get("pr_url"):
        parts += ["", f"Landed in {wo['pr_url']}."]
    else:
        parts += ["", "Closed with no pull request: this work order produced no code to "
                      "land."]
    summary = (wo.get("result_summary") or "").strip()
    if summary:
        clipped = summary[:SUMMARY_CHARS]
        parts += ["", clipped + ("…" if len(summary) > SUMMARY_CHARS else "")]
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

    Two conditions, and both are the project's own: its git `origin` IS that repository,
    and it has opted in with `bugs.auto_work_order`. The project that NOTICED the bug has
    no say — every worker in the fleet carries the `report-jarvis-bug` skill, so the only
    consent that means anything is the consent of whoever pays for the work (`catalog.
    BugsConfig`).

    The `git remote` read costs one subprocess per OPTED-IN project, which is normally
    zero: the flag is checked first, so a fleet that has not turned this on never shells
    out at all.
    """
    from . import github

    want = (repo or bug_repo()).lower()
    for spec in getattr(catalog, "projects", []):
        if not spec.bugs.auto_work_order:
            continue
        origin = github.origin_repo(spec.path)
        if origin and f"{origin[0]}/{origin[1]}" == want:
            return spec
    return None


#: What the worker is told to do, above the issue body. The worker sees ONLY the
#: description, so the issue URL has to be in it — `jarvis bug report` is the only thing
#: that knows the two belong together.
WORK_ORDER_BRIEF = """\
Fix {url}.

The issue text is reproduced below; it is what the reporting agent saw. Read the issue \
itself first if anything since then has been added to it.

When this work lands, Jarvis closes the issue itself — do not close it by hand, and do \
not open a second issue for the same thing.

---

{body}"""


def work_order_for(issue_url: str, title: str, body: str,
                   repo: str | None = None) -> dict[str, Any]:
    """File the work order that will fix this freshly created issue.

    Returns what actually happened, always — never raises for a fleet that simply has
    not opted in. `wo_id` is empty and `reason` says why, which is what
    `bugreport.report_bug` puts in front of the user instead of a claim it cannot back
    (its docstring's rule, applied to the rest of the lifecycle).

    **NO SECOND WORK ORDER FOR ONE ISSUE** (#240 D). An issue that already has a LIVE
    work order hands that one back; one whose work orders have all settled is free to get
    another, which is exactly what a reopened issue needs.
    """
    from .catalog import CatalogError
    from .ops import OpsError, create_work_order, registered_project_paths
    from .project_store import TERMINAL_STATUSES, ProjectStore

    try:
        from .ops import resolve_catalog
        spec = tracker_project(resolve_catalog(), repo)
    except (CatalogError, OpsError, FileNotFoundError) as e:
        return {"wo_id": "", "project": "", "created": False,
                "reason": f"the fleet catalog could not be read ({e})"}
    if spec is None:
        return {"wo_id": "", "project": "", "created": False,
                "reason": (f"no project has `bugs.auto_work_order` on for "
                           f"{repo or bug_repo()}, so the issue was filed and left "
                           f"for a human")}
    if spec.name not in registered_project_paths():
        return {"wo_id": "", "project": spec.name, "created": False,
                "reason": (f"project {spec.name!r} is in the catalog but not registered "
                           f"— run `jarvis start` before bugs can be picked up")}

    store = ProjectStore(spec.path)
    try:
        for existing in store.work_orders_for_issue(issue_url):
            if existing["status"] not in TERMINAL_STATUSES:
                return {"wo_id": existing["id"], "project": spec.name, "created": False,
                        "reason": "an open work order already covers this issue"}
    finally:
        store.close()

    wo = create_work_order(
        spec.name, title,
        description=WORK_ORDER_BRIEF.format(url=issue_url, body=body),
        origin="jarvis", issue_url=issue_url)
    out = {"wo_id": wo["id"], "project": spec.name, "created": True, "reason": ""}
    # Label it HERE and not only on the next sweep: `jarvis bug report` returns to an
    # agent that is about to tell the user the issue is being worked on, and the tracker
    # should already say so. A failure is not fatal and not hidden — the sweep retries
    # it, and the reason travels back so nothing claims a state it did not reach.
    store = ProjectStore(spec.path)
    try:
        out["issue_state"] = record_applied(store, wo, spec.bugs.label)
    except GitHubError as e:
        out["issue_state"] = ""
        out["label_error"] = str(e)
    finally:
        store.close()
    return out


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
