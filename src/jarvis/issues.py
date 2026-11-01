"""Writing to the tracker: the second half of a bug the OS filed itself (#240)"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .bugreport import bug_repo, gh_bin, gh_missing_message
from .github import GhUnavailable, GitHubError

log = logging.getLogger("jarvis.issues")

#: Every `gh` command this module builds, as the (verb, subverb) pair that opens the argument list
ISSUE_VERBS = (("issue", "view"), ("issue", "edit"), ("issue", "comment"),
               ("issue", "close"), ("label", "create"),
               # Added deliberately, in the commit that also edited the AST test
               ("issue", "create"), ("issue", "list"),
               # `issue reopen` is the recurrence path's write (spec §8): the OS links a gap that came back to its...
               ("issue", "reopen"),
               # A READ, in a list that is otherwise writes — same shape as `("issue", "list")` above
               ("repo", "view"),
               # A READ of the AUTHENTICATED ACCOUNT, against no repository at all: `gh_login`
               ("api", "user"))

#: One round trip against GitHub's API
GH_TIMEOUT = 30

#: THE ONLY HOST this OS writes an issue to
ISSUE_HOST = "github.com"

#: The ONLY shape an issue URL may have before it becomes an argument
ISSUE_URL_RE = re.compile(
    r"^https://([A-Za-z0-9.-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/issues/([0-9]+)$")

#: The three tracker states a work order can want its issue to be in (#240)
IN_PROGRESS = "in-progress"
RELEASED = "released"
CLOSED = "closed"

#: What an untracked work order reports, so callers never have to special-case None
UNTRACKED = ""

#: The colour and blurb a freshly created label gets
LABEL_COLOUR = "0e8a16"
LABEL_DESCRIPTION = "A Jarvis work order is on this."


# -- priority: the only thing that routes a filed bug

#: Lowest to highest
PRIORITIES = ("low", "medium", "high", "critical", "blocker")

#: The two that spend money
DISPATCHING_PRIORITIES = ("critical", "blocker")

#: The highest level that commits the fleet to nothing
SAFE_DOWNGRADE = "high"


def priority_label(level: str) -> str:
    """The tracker's own copy of the priority, so it shows without opening the OS"""
    return f"priority: {checked_priority(level)}"


#: Every priority label, for the sweep that has to take the old one off before putting the new one on
PRIORITY_LABELS = tuple(f"priority: {p}" for p in PRIORITIES)

#: WHAT THE LEVELS MEAN, written once and shown wherever a level is asked for or judged: the `jarvis...
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
    """`level` if it is one of the five"""
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
    """Where a Neo downgrade lands, given what (if anything) Neo named"""
    try:
        level = checked_priority(stated)
    except ValueError:
        return SAFE_DOWNGRADE
    if PRIORITIES.index(level) >= PRIORITIES.index(checked_priority(claimed)):
        return SAFE_DOWNGRADE
    return level


class IssueLifecycleError(GitHubError):
    """The tracker could not be moved, and nothing about it was changed"""


@dataclass(frozen=True)
class Issue:
    """What `gh issue view --json state,labels,number,title,url` says, and nothing more"""

    url: str
    number: int
    #: GitHub's own enum, uppercased: OPEN or CLOSED
    state: str
    labels: tuple[str, ...]
    #: The issue's own headline
    title: str = ""

    @property
    def closed(self) -> bool:
        return self.state == "CLOSED"

    def has(self, label: str) -> bool:
        """Case-insensitively, because GitHub matches labels that way and a catalog that spells the label `In..."""
        low = label.strip().lower()
        return any(l.lower() == low for l in self.labels)


def issue_number(url: str) -> int:
    """The issue's number, or 0 if `url` is not an issue URL at all"""
    match = ISSUE_URL_RE.match(url or "")
    return int(match.group(4)) if match else 0


def checked_issue_url(url: str, repo: str | None = None) -> str:
    """`url` if the OS may write to it"""
    match = ISSUE_URL_RE.match(url or "")
    if match is None:
        raise IssueLifecycleError(
            f"{url!r} is not an issue URL this OS will write to",
            GitHubError.URL_REFUSED)
    want = (repo or bug_repo()).lower()
    # THE HOST COUNTS, and is checked before the repository so the message names the actual objection
    if match.group(1).lower() != ISSUE_HOST:
        raise IssueLifecycleError(
            f"{url!r} is on {match.group(1)}, but this OS only writes to {ISSUE_HOST}",
            GitHubError.URL_REFUSED)
    got = f"{match.group(2)}/{match.group(3)}".lower()
    if got != want:
        raise IssueLifecycleError(
            f"{url!r} is on {got}, but this OS only writes to {want}",
            GitHubError.URL_REFUSED)
    return url


#: The ONLY shape a label may have before it becomes an argument
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$")


#: The ONLY shape a repository may have before it becomes a `--repo` argument
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def checked_repo(repo: str) -> str:
    """`repo` if it may become a `gh --repo` argument"""
    if not REPO_RE.match(repo or ""):
        raise IssueLifecycleError(
            f"{repo!r} is not a repository this OS will send to GitHub — it must be "
            f"`owner/name`, starting with a letter or digit",
            GitHubError.URL_REFUSED)
    return repo


def checked_label(label: str) -> str:
    """`label` if it may become a `gh` argument"""
    if not LABEL_RE.match(label or ""):
        raise IssueLifecycleError(
            f"{label!r} is not a label this OS will send to GitHub — it must start with "
            f"a letter or digit and use only letters, digits, spaces and ._:/-",
            GitHubError.URL_REFUSED)
    return label


def _run(args: list[str], *, url: str, tolerate: str = "",
         stdin: str | None = None) -> str:
    """One `gh` call"""
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
        # The remote text is LOGGED, never raised into `reason` — `github.GitHubError`
        log.info("%s failed: %s", shown, detail)
        raise IssueLifecycleError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def view(url: str, repo: str | None = None) -> Issue:
    """Read the issue at `url`"""
    url = checked_issue_url(url, repo)
    stdout = _run(["issue", "view", url, "--json", "number,state,labels,title,url"],
                  url=url)
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
                 state=state, labels=labels, title=str(payload.get("title") or ""))


def ensure_label(label: str, repo: str | None = None) -> None:
    """Make sure the repository carries `label`, so `--add-label` cannot fail over it"""
    repo = checked_repo(repo or bug_repo())
    _run(["label", "create", checked_label(label), "--repo", repo,
          "--color", LABEL_COLOUR, "--description", LABEL_DESCRIPTION],
         url=repo, tolerate="already exists")


def add_label(url: str, label: str, repo: str | None = None) -> None:
    _run(["issue", "edit", checked_issue_url(url, repo), "--add-label",
          checked_label(label)], url=url)


def remove_label(url: str, label: str, repo: str | None = None) -> None:
    _run(["issue", "edit", checked_issue_url(url, repo), "--remove-label",
          checked_label(label)], url=url)


def comment(url: str, body: str, repo: str | None = None) -> None:
    """Add a comment, with the body on stdin rather than in argv"""
    url = checked_issue_url(url, repo)
    _run(["issue", "comment", url, "--body-file", "-"], url=url, stdin=body)


def reopen(url: str, body: str) -> None:
    """Reopen the issue, carrying `body` as the comment that explains why"""
    _run(["issue", "reopen", checked_issue_url(url), "--comment", body], url=url)


def close(url: str, body: str) -> None:
    """Close the issue, carrying `body` as the closing comment"""
    _run(["issue", "close", checked_issue_url(url), "--comment", body], url=url)


# -- what the tracker should say, given what the work order is doing


# -- validation follow-ups: the panel's non-blocking findings, on the project's tracker

#: What marks an issue as the panel's rather than a person's
FOLLOW_UP_LABEL = "validation follow-up"

#: The colour and blurb `FOLLOW_UP_LABEL` is created with
FOLLOW_UP_COLOUR = "c5def5"
FOLLOW_UP_DESCRIPTION = "Raised by the Jarvis validation panel; not a blocker."


def repo_is_private(repo: str) -> bool:
    """True only when GitHub SAID this repository is private; False on any doubt (docs/superpowers/specs/2026-09-14-a-filed-bug-runs-itself.md)"""
    try:
        stdout = _run(["repo", "view", checked_repo(repo), "--json", "isPrivate"],
                      url=repo)
        return json.loads(stdout or "").get("isPrivate") is True
    except Exception as e:  # noqa: BLE001 — every failure has the same safe answer
        log.info("could not establish whether %s is private (%s) — treating it as "
                 "public, so no finding text is published there", repo, e)
        return False


def file_follow_up(repo: str, title: str, body: str) -> str:
    """File one follow-up on `repo` and return its issue URL"""
    repo = checked_repo(repo)
    ensure_follow_up_label(repo)
    stdout = _run(["issue", "create", "--repo", repo, "--title", title,
                   "--label", FOLLOW_UP_LABEL, "--body-file", "-"],
                  url=repo, stdin=body)
    url = (stdout or "").strip().splitlines()[-1] if (stdout or "").strip() else ""
    # Held to the shape AND to the repository we just named
    return checked_issue_url(url, repo)


def ensure_follow_up_label(repo: str) -> None:
    """`ensure_label` with the follow-up's own colour and blurb"""
    repo = checked_repo(repo)
    _run(["label", "create", checked_label(FOLLOW_UP_LABEL), "--repo", repo,
          "--color", FOLLOW_UP_COLOUR, "--description", FOLLOW_UP_DESCRIPTION],
         url=repo, tolerate="already exists")


def issues_mentioning(repo: str, term: str) -> list[dict[str, Any]]:
    """Every issue on `repo` whose BODY mentions `term`, open or closed (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    repo = checked_repo(repo)
    term = (term or "").strip()
    if not term:
        return []
    stdout = _run(["issue", "list", "--repo", repo, "--state", "all",
                   "--search", f"{term} in:body", "--limit", "100",
                   # `author` and `body` are ADDITIVE: the red-base adoption filter needs both to tell the OS's own...
                   "--json", "number,title,url,state,author,body"], url=repo)
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("url")]


#: `gh_login`'s process cache, holding at most the one answer
_GH_LOGIN: list[str] = []


def reset_gh_login() -> None:
    """Forget the cached login"""
    _GH_LOGIN.clear()


def gh_login() -> str:
    """The login `gh` is authenticated as — the account the OS files issues under"""
    if _GH_LOGIN:
        return _GH_LOGIN[0]
    try:
        login = _run(["api", "user", "--jq", ".login"], url="the authenticated account")
    except Exception:  # noqa: BLE001 — an unknown login adopts nothing
        log.info("could not read the `gh`-authenticated login")
        return ""
    _GH_LOGIN.append(login.strip())
    return _GH_LOGIN[0]


#: `viewerPermission` levels that may close and label an issue; READ can only comment (#1346)
TRACKER_WRITE_PERMISSIONS = ("ADMIN", "MAINTAIN", "WRITE", "TRIAGE")

#: The lowest of them, as the inbox and doctor name it
TRACKER_NEEDED_PERMISSION = "TRIAGE"

#: How long one access answer is trusted
TRACKER_ACCESS_TTL = 600


@dataclass(frozen=True)
class TrackerAccess:
    """What `gh` can do on one tracker, as GitHub answered it (#1346)"""

    repo: str
    login: str
    #: GitHub's `viewerPermission`, or "" when it could not be read
    permission: str
    error: str = ""

    @property
    def denied(self) -> bool:
        """True only on a DEFINITE answer below triage; an unreadable probe is unknown"""
        return bool(self.permission) and self.permission not in TRACKER_WRITE_PERMISSIONS

    def explain(self) -> str:
        """The account, the repo, what it lacks, and how to point `gh` elsewhere"""
        config = os.environ.get("GH_CONFIG_DIR") or "the default gh config (~/.config/gh)"
        return (f"`gh` is authenticated as {self.login or 'an unknown account'}, which has "
                f"{self.permission} permission on {self.repo}; closing and labelling issues "
                f"needs {TRACKER_NEEDED_PERMISSION} or higher. Jarvis has stopped writing to "
                f"that tracker. This process reads {config}: set GH_CONFIG_DIR (or GH_TOKEN) "
                f"in the daemon's service environment to the account that owns the tracker, "
                f"or grant {self.login or 'that account'} triage access, then restart the "
                f"daemon.")


#: `tracker_access`'s process cache: repo to (answered at, answer)
_TRACKER_ACCESS: dict[str, tuple[float, TrackerAccess]] = {}


def reset_tracker_access() -> None:
    """Forget every cached access answer"""
    _TRACKER_ACCESS.clear()


def tracker_access(repo: str) -> TrackerAccess:
    """Who `gh` writes to `repo` as, and with what permission (#1346)"""
    repo = checked_repo(repo)
    key = repo.lower()
    cached = _TRACKER_ACCESS.get(key)
    if cached and time.time() - cached[0] < TRACKER_ACCESS_TTL:
        return cached[1]
    try:
        stdout = _run(["repo", "view", repo, "--json", "viewerPermission"], url=repo)
        permission = str(json.loads(stdout or "").get("viewerPermission") or "").upper()
    except Exception as e:  # noqa: BLE001 — unknown, never a denial (kn-40db1828); not cached
        return TrackerAccess(repo=repo, login=gh_login(), permission="", error=str(e))
    answer = TrackerAccess(repo=repo, login=gh_login(), permission=permission)
    _TRACKER_ACCESS[key] = (time.time(), answer)
    return answer


def follow_ups_filed(repo: str, unit_id: str) -> list[dict[str, Any]]:
    """Every follow-up the panel has already filed on `repo` for one unit"""
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


# -- recurrences: the gap came back and the OS already had a rule for it

#: What marks the original issue as a regression rather than a fresh report
REGRESSION_LABEL = "regression"

#: Its own colour and blurb, distinct from the bug label's green and the follow-up's blue
REGRESSION_COLOUR = "d93f0b"
REGRESSION_DESCRIPTION = ("This gap recurred: Jarvis already had a detector for it and "
                          "the rule did not hold.")


def ensure_regression_label(repo: str) -> None:
    """`ensure_label` with the regression's own colour and blurb"""
    repo = checked_repo(repo)
    _run(["label", "create", checked_label(REGRESSION_LABEL), "--repo", repo,
          "--color", REGRESSION_COLOUR, "--description", REGRESSION_DESCRIPTION],
         url=repo, tolerate="already exists")


def recurrence_issues(detector_id: str, gap_class: str,
                      repo: str | None = None) -> list[dict[str, Any]]:
    """Issues on the tracker about this DETECTOR and this GAP CLASS, open or closed"""
    repo = checked_repo(repo or bug_repo())
    stdout = _run(["issue", "list", "--repo", repo, "--state", "all",
                   "--search", f"{detector_id} {gap_class} in:body", "--limit", "100",
                   "--json", "number,title,url,state"], url=repo)
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("url")]


# -- back-references: how many work orders have pointed at this issue

#: What the count is written as, both here and on the tracker
REFERENCE_LABEL_PREFIX = "referenced: "

#: Matches any reference label, whatever the number
REFERENCE_LABEL_RE = re.compile(r"^referenced: [0-9]+$")

REFERENCE_LABEL_COLOUR = "d4c5f9"
REFERENCE_LABEL_DESCRIPTION = "How many Jarvis work orders have pointed at this issue."


def reference_label(count: int) -> str:
    return f"{REFERENCE_LABEL_PREFIX}{count}"


def set_reference_label(url: str, count: int, repo: str) -> None:
    """Make the issue's reference label say `count`, and say it once"""
    issue = view(url, repo)
    if issue.closed:
        return
    want = reference_label(count)
    for stale in issue.labels:
        if REFERENCE_LABEL_RE.match(stale.strip().lower()) and stale.strip() != want:
            remove_label(url, stale, repo)
    if not issue.has(want):
        ensure_reference_label(repo, count)
        add_label(url, want, repo)


def ensure_reference_label(repo: str, count: int) -> None:
    """Define `referenced: <count>` on `repo`, so `--add-label` cannot fail over it"""
    repo = checked_repo(repo)
    _run(["label", "create", checked_label(reference_label(count)), "--repo", repo,
          "--color", REFERENCE_LABEL_COLOUR, "--description",
          REFERENCE_LABEL_DESCRIPTION], url=repo, tolerate="already exists")


def reference_comment(unit_id: str, project: str, kind: str, title: str = "") -> str:
    """What the issue says when a further work order points at it"""
    how = {"raised": "raised this finding",
           "assigned": "was dispatched to fix this",
           "cited": "cites this issue in its brief"}.get(kind, "references this issue")
    line = f"Jarvis work order `{unit_id}` ({project}) {how}."
    if title:
        line += f" — {title}"
    return "\n".join([
        line, "",
        "<!-- Recorded automatically by the Jarvis issue-reference index. -->"])


def desired_state(store: Any, wo: dict[str, Any]) -> str:
    """Where this work order says its issue belongs (#240)"""
    from .project_store import OPEN_STATUSES

    wo_id = wo["id"]
    if wo["status"] in OPEN_STATUSES:
        return RELEASED if store.pr_closure_told(wo_id) else IN_PROGRESS
    if wo["status"] == "completed" and not store.work_unlanded_open(wo_id):
        return CLOSED
    return RELEASED


def needs_sync(store: Any, wo: dict[str, Any]) -> bool:
    """Is there anything left the sweep MAY do to this issue? (#961)"""
    if (wo.get("issue_state") or "") == CLOSED:
        return False
    return desired_state(store, wo) != (wo.get("issue_state") or "")


def closing_comment(wo: dict[str, Any]) -> str:
    """What the issue says about its own closure, months later (#240)"""
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
    """Move this work order's issue to where `desired_state` says it belongs"""
    url = checked_issue_url(wo.get("issue_url") or "")
    want = desired_state(store, wo)
    issue = view(url)

    if want == CLOSED:
        if issue.has(label):
            remove_label(url, label)
        if issue.closed:
            # Someone closed it first (#240)
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


# -- picking the issue up: the step between "filed" and "being worked on"


def tracker_project(catalog: Any, repo: str | None = None) -> Any:
    """The project that would FIX a bug on `repo`, or None if none would"""
    from .ops import follow_up_repo

    want = (repo or bug_repo()).lower()
    for spec in getattr(catalog, "projects", []):
        bugs = getattr(spec, "bugs", None)
        if follow_up_repo(spec.path, getattr(bugs, "repo", "")).lower() == want:
            return spec
    return None


#: What the worker is told to do, above the issue body
WORK_ORDER_BRIEF = """\
Fix {url}.

{why}

The issue text is reproduced below; it is what the reporting agent saw. Read the issue \
itself first if anything since then has been added to it.

When this work lands, Jarvis closes the issue itself — do not close it by hand, and do \
not open a second issue for the same thing.

---

{body}"""

#: `WORK_ORDER_BRIEF`'s `why`, one per route in
TRIAGED_WHY = """\
This bug is `{priority}`, confirmed by Neo against the OS's own rubric, which is why it \
became a work order at all. A release goes out once your fix LANDS."""
STARTED_WHY = """\
This bug is `{priority}` on the tracker and the user started work on it themselves."""
EXPEDITED_WHY = """\
This bug is `{priority}` on the tracker and the filing EXPEDITED it: work starts now, \
whatever the rating says.{tail}"""

#: The key under an expedited work order's `metadata` that says the USER asked for this in production...
EXPEDITED_KEY = "expedited"

#: `EXPEDITED_WHY`'s tail on a `critical`/`blocker` filing
EXPEDITED_TRIAGE_TAIL = """ That rating is a CLAIM and Neo is re-assessing it in \
parallel, so this work order carries no rating of its own yet — if the fix lands first, \
it lands without the release a confirmed `blocker` would have shipped. Neo confirming \
writes the rating on, and a release follows then. None of that is yours to wait for: \
the work is yours to do now."""

#: THE ROUTE OUT, written once
START_COMMAND = "jarvis issues start {url}"

#: Keys a triage question's context carries when an investigation GAP filed the bug (#1344)
INVESTIGATION_KEY = "investigation"
MORPHEUS_ITEM_KEY = "morpheus_item"
#: The pre-triage dispatch line's tail; still on the record after Neo answered = a lost echo (#1402)
INVESTIGATION_PENDING_MARK = "Neo is re-assessing it; the fix starts at once"

def self_dispatch(catalog: Any) -> bool:
    """`os.autonomy.self_dispatch`: may a confirmed claim dispatch work nobody typed?"""
    autonomy = getattr(getattr(catalog, "os", None), "autonomy", None)
    return bool(getattr(autonomy, "self_dispatch", False))


def self_dispatch_configured() -> bool:
    """`self_dispatch` of the resolved fleet catalog; an unreadable catalog reads as off"""
    from .ops import resolve_catalog
    try:
        return self_dispatch(resolve_catalog())
    except Exception:  # noqa: BLE001 — fail closed: no catalog, no self-dispatch
        return False


def route_filing(issue_url: str, title: str, body: str, priority: str,
                 repo: str | None = None, expedite: bool = False,
                 investigation: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything that happens to a freshly filed bug, before anyone is told about it"""
    from .catalog import CatalogError
    from .central_store import CentralStore
    from .ops import OpsError, registered_project_paths, resolve_catalog

    out: dict[str, Any] = {"priority": priority, "wo_id": "", "backlog_id": "",
                           "project": "", "neo_question_id": 0, "reason": ""}
    try:
        catalog = resolve_catalog()
        spec = tracker_project(catalog, repo)
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

    # The tracker's own copy of the level
    try:
        set_priority_label(issue_url, priority)
    except GitHubError as e:
        out["label_error"] = str(e)

    if expedite:
        # Before the triage question, so the work order exists whatever Neo does next
        try:
            out["wo_id"] = promote_confirmed(
                spec, {"issue_url": issue_url, "title": title, "body": body,
                       # NO RELEASE OFF AN UNCONFIRMED CLAIM
                       "priority": "" if dispatches(priority) else priority},
                metadata={EXPEDITED_KEY: True}, origin="jarvis",
                why=EXPEDITED_WHY.format(
                    priority=priority,
                    tail=EXPEDITED_TRIAGE_TAIL if dispatches(priority) else ""))
            out["expedited"] = True
            out["reason"] = (f"`{priority}` expedited — work order {out['wo_id']} in "
                             f"{spec.name} is on it now")
        except Exception as e:  # noqa: BLE001 — the issue exists whatever happens here
            out["reason"] = (f"`{priority}` expedited but the work order could NOT be "
                             f"created ({e}) — start it yourself with "
                             f"`{START_COMMAND.format(url=issue_url)}`")

    # An investigation's GAP is triaged at ANY level: Neo agreeing plus the steward starts it (#1344)
    if not dispatches(priority) and not investigation:
        if not expedite:
            out["reason"] = (f"`{priority}` is on the tracker for the user to pick up: "
                             f"`{START_COMMAND.format(url=issue_url)}`")
        return out

    # `expedite` appends to the sentence above; a plain filing has none and writes its own
    try:
        out["neo_question_id"] = ask_triage(spec.name, issue_url, title, body, priority,
                                            extra=investigation)
        if investigation and not expedite:
            out["reason"] = (f"`{priority}` filed by {investigation.get(INVESTIGATION_KEY)} "
                             f"— {INVESTIGATION_PENDING_MARK} if Neo "
                             f"agrees and the steward allows it")
        elif expedite:
            out["reason"] += (f"; Neo is re-assessing the `{priority}` claim in "
                              f"parallel and can still move the rating, not the work")
        elif not self_dispatch(catalog):
            out["reason"] = (f"`{priority}` claimed — Neo is re-assessing the rating; "
                             f"nothing is dispatched either way, start it yourself with "
                             f"`{START_COMMAND.format(url=issue_url)}`")
        else:
            out["reason"] = (f"`{priority}` claimed — Neo is re-assessing it against the "
                             f"rubric; a work order is created only if Neo confirms")
    except Exception as e:  # noqa: BLE001 — the issue exists whatever happens here
        if expedite:
            out["reason"] += (f"; the `{priority}` claim was NOT re-assessed — Neo could "
                              f"not be asked ({e}), so the `priority:` label is the "
                              f"filing agent's own")
        else:
            out["reason"] = (f"`{priority}` claimed but NOT confirmed — Neo could not be "
                             f"asked ({e}). Nothing was dispatched; start it yourself "
                             f"with `{START_COMMAND.format(url=issue_url)}`.")
    return out


def set_priority_label(issue_url: str, level: str) -> None:
    """Make the issue carry exactly one priority label: this one"""
    want = priority_label(level)
    issue = view(issue_url)
    for stale in PRIORITY_LABELS:
        if stale != want and issue.has(stale):
            remove_label(issue_url, stale)
    if not issue.has(want):
        ensure_label(want)
        add_label(issue_url, want)


#: The question Neo re-assesses a `critical`/`blocker` claim with
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


def ask_triage(project: str, issue_url: str, title: str, body: str,
               priority: str, extra: dict[str, str] | None = None) -> int:
    """Queue the re-assessment; `extra` rides in the context back to `settle_triage`"""
    from .neo_store import NeoStore
    from .provenance import TITLE_LIMIT, Borrowed, borrowed_block

    # The title is bounded in the JSON record too: it rides to Neo as `context=`
    context = json.dumps({**(extra or {}), "issue_url": issue_url,
                          "title": title[:TITLE_LIMIT], "priority": priority,
                          "body": body[:4000]})
    # Decision #11: the title is borrowed text
    quoted_title = borrowed_block(Borrowed(
        label="the tracker issue's title", whose="the agent that filed the bug",
        text=title, limit=TITLE_LIMIT))
    neo = NeoStore()
    try:
        q = neo.ask(project, "", TRIAGE_QUESTION.format(
            priority=priority, rubric=PRIORITY_RUBRIC, url=issue_url,
            title="\n" + quoted_title,
            levels=", ".join(f"`{p}`" for p in PRIORITIES), body=body[:4000]),
            context=context, kind="triage")
    finally:
        neo.close()
    return int(q["id"])


def triage_payload(q: dict[str, Any]) -> dict[str, Any]:
    """The filing facts back out of a triage question, or `{}` if it is not one"""
    if q.get("kind") != "triage":
        return {}
    try:
        payload = json.loads(q.get("context") or "")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("issue_url") else {}


#: What the tracker records about the re-assessment
TRIAGE_COMMENT = """\
**Priority re-assessed by Neo:** filed as `{claimed}` → **`{settled}`**.

{tail}

<!-- Jarvis bug lifecycle (issue #240): the filing agent's rating is a claim; Neo
     re-assesses `critical` and `blocker` against the OS's rubric before any work is
     dispatched. The reasoning is on the work-order record, not here. -->"""


def triage_comment(claimed: str, settled: str, tail: str) -> str:
    return TRIAGE_COMMENT.format(claimed=claimed, settled=settled, tail=tail)


def record_applied(store: Any, wo: dict[str, Any], label: str) -> str:
    """`apply`, plus the two writes that stop it being asked again for nothing (#240)"""
    applied = apply(store, wo, label)
    store.update_work_order(wo["id"], issue_state=applied)
    store.add_event(wo["id"], f"issue_{applied.replace('-', '_')}",
                    {"issue_url": wo.get("issue_url"), "label": label})
    return applied


# -- acting on the verdict


def read_triage_verdict(claimed: str, verdict: dict[str, Any]) -> tuple[str, str]:
    """`(outcome, settled)` — what Neo actually ruled on a priority claim"""
    from .neo import _VERDICT_ALIASES

    stated = ((verdict.get("answer") or "").strip().split() or [""])[0]
    stated = stated.strip("`.,:;*").lower()
    said = _VERDICT_ALIASES.get(stated) or ""

    if verdict.get("approve"):
        return ("confirmed", claimed)
    if verdict.get("verdict_stated"):
        # An explicit deny whose answer opens with "approve" is a reply contradicting itself
        return (("unconfirmed", claimed) if said == "approved"
                else ("downgraded", downgrade_to(claimed, stated)))

    # No verdict field at all: `approve` is False only because that is what it defaults to
    if said == "approved":
        return ("confirmed", claimed)
    if said in ("denied", "dismissed"):
        return ("downgraded", downgrade_to(claimed, stated))
    # `downgrade_to` OWNS THE ORDERING (review round 1): it already answers "is this a level below the...
    return (("downgraded", stated) if downgrade_to(claimed, stated) == stated
            else ("unconfirmed", claimed))


def settle_triage(catalog: Any, q: dict[str, Any], verdict: dict[str, Any]
                  ) -> dict[str, Any]:
    """Route a bug now that Neo has ruled on its priority"""
    payload = triage_payload(q)
    if not payload:
        return {"outcome": "not-a-triage"}
    out = _settle_triage(catalog, q, verdict, payload)
    inv_id = str(payload.get(INVESTIGATION_KEY) or "")
    if inv_id:
        from .ops import record_investigation_dispatch

        out["dispatch"] = investigation_dispatch_note(out)
        try:
            # By id alone: the investigation lives in the investigated project, not the tracker's (#1402)
            record_investigation_dispatch(inv_id, out.get("existing_wo_id") or "",
                                          out["dispatch"])
        except Exception:  # noqa: BLE001 — the settlement stands; INV-INVESTIGATION-DISPATCH re-derives the echo
            log.exception("could not record the dispatch on %s", inv_id)
    return out


def investigation_dispatch_note(out: dict[str, Any]) -> str:
    """Who let an investigation's fix start, or who refused it and why (#1344)"""
    start = f"start it by hand: `{START_COMMAND.format(url=out['issue_url'])}`"
    if out.get("wo_id"):
        return (f"started at once as {out['wo_id']}: Neo confirmed `{out['settled']}` "
                f"and the steward allowed it")
    if out.get("existing_wo_id"):
        return f"{out['existing_wo_id']} was already on it"
    if out["outcome"] != "confirmed":
        why = (out.get("reason") or "no reason given").strip()[:300]
        return (f"NOT started — Neo did not agree ({out['outcome']}, "
                f"`{out['claimed']}` -> `{out['settled']}`: {why}); {start}")
    return f"NOT started — the steward refused ({out.get('steward') or 'no decision'}); {start}"


def _settle_triage(catalog: Any, q: dict[str, Any], verdict: dict[str, Any],
                   payload: dict[str, Any]) -> dict[str, Any]:
    from .central_store import CentralStore

    claimed = payload.get("priority") or ""
    url = payload["issue_url"]
    out: dict[str, Any] = {"outcome": "", "claimed": claimed, "settled": claimed,
                           "issue_url": url, "wo_id": "",
                           "backlog_id": payload.get("backlog_id") or ""}

    # In full and on every outcome: this is the private half of the record
    out["reason"] = verdict.get("reason") or ""

    if verdict.get("escalate") or verdict.get("failed"):
        out["outcome"] = "unconfirmed"
        return out

    out["outcome"], out["settled"] = read_triage_verdict(claimed, verdict)
    if out["outcome"] == "unconfirmed":
        return out

    spec = next((p for p in getattr(catalog, "projects", [])
                 if p.name == q.get("project")), None)
    if spec is None:
        out["outcome"] = "unconfirmed"
        out["reason"] = f"project {q.get('project')!r} is no longer in the catalog"
        return out

    from . import steward
    from .project_store import ProjectStore

    decision = None
    # The fix order points back at the investigation and the validation it came from (#1344)
    links = {k: payload[k] for k in (INVESTIGATION_KEY, MORPHEUS_ITEM_KEY) if payload.get(k)}
    if out["outcome"] == "confirmed":
        # The steward's call (spec 2026-10-21 §3.2): Neo's confirmation IS the evidence a...

        store = ProjectStore(spec.path)
        try:
            decision = steward.gate(
                steward.Candidate("investigation" if links else "bug_agent",
                                  out["settled"], spec.name,
                                  payload.get("title") or url,
                                  evidence=(f"neo question {q.get('id')} confirmed",),
                                  parent_wo_id=str(q.get("wo_id") or "")),
                catalog=catalog, store=store, self_dispatch=self_dispatch(catalog))
        finally:
            store.close()
        out["steward"] = f"{decision.outcome}, {decision.rule}: {decision.reason}"
        if decision.outcome == "dispatch" and not decision.allowed:
            out["steward"] += "; os.autonomy.self_dispatch is off"
    if decision is not None and decision.allowed:
        out["wo_id"] = promote_confirmed(
            spec, payload, metadata=steward.stamped(links, decision),
            budget_usd=decision.budget_usd)
        steward.linked(decision, out["wo_id"])
        if out["backlog_id"]:
            # Only a question asked BEFORE the backlog item stopped being created can have one
            central = CentralStore()
            try:
                central.mark_backlog(out["backlog_id"], "promoted",
                                     promoted_wo_id=out["wo_id"])
            finally:
                central.close()

    out["existing_wo_id"] = out["wo_id"] or live_work_order(spec, url)
    if out["existing_wo_id"]:
        # NEVER ONLY THE LABEL
        out["repriced_wo_id"] = settle_work_order_priority(
            spec, out["existing_wo_id"], claimed, out["settled"])

    # LAST, and after everything that touches the work order: a tracker comment that failed must not undo...
    try:
        set_priority_label(url, out["settled"])
        comment(url, triage_comment(
            claimed, out["settled"],
            f"A work order is on it: `{out['existing_wo_id']}`."
            if out["existing_wo_id"] else
            f"No work order was created. Start one with "
            f"`{START_COMMAND.format(url=url)}`."))
    except GitHubError as e:
        out["comment_error"] = str(e)
    return out


def settle_work_order_priority(spec: Any, wo_id: str, claimed: str,
                               settled: str) -> str:
    """Write the level Neo SETTLED on onto a live work order"""
    from .project_store import ProjectStore

    store = ProjectStore(spec.path)
    try:
        try:
            carried = store.get_work_order(wo_id).get("issue_priority") or ""
        except KeyError:
            return ""
        if carried == settled:
            return ""
        store.update_work_order(wo_id, issue_priority=settled)
        store.add_event(wo_id, "triage_settled",
                        {"claimed": claimed, "settled": settled, "carried": carried})
    finally:
        store.close()
    return wo_id


def live_work_order(spec: Any, issue_url: str) -> str:
    """The id of the non-terminal work order on this issue, or `""` (#240)"""
    from .project_store import TERMINAL_STATUSES, ProjectStore

    store = ProjectStore(spec.path)
    try:
        for row in store.work_orders_for_issue(issue_url):
            if row["status"] not in TERMINAL_STATUSES:
                return str(row["id"])
    finally:
        store.close()
    return ""


def was_expedited(wo: dict[str, Any]) -> bool:
    """Did the user expedite this work order's filing?"""
    from . import db

    return bool((db.from_json(wo.get("metadata"), {}) or {}).get(EXPEDITED_KEY))


def promote_confirmed(spec: Any, payload: dict[str, Any], why: str = "",
                      metadata: dict[str, Any] | None = None,
                      budget_usd: float | None = None, origin: str = "os") -> str:
    """Create the work order a confirmed `critical`/`blocker` earned (#240)"""
    from .ops import create_work_order
    from .project_store import ProjectStore

    url = payload["issue_url"]
    priority = payload.get("priority") or ""
    existing = live_work_order(spec, url)
    if existing:
        return existing

    wo = create_work_order(
        spec.name, payload.get("title") or url,
        description=WORK_ORDER_BRIEF.format(
            url=url, body=payload.get("body") or "",
            why=why or TRIAGED_WHY.format(priority=priority)),
        origin=origin, issue_url=url, issue_priority=priority, metadata=metadata,
        budget_usd=budget_usd)
    store = ProjectStore(spec.path)
    try:
        # Label it now rather than only on the next sweep: the promotion and the tracker saying so should not...
        try:
            record_applied(store, store.get_work_order(wo["id"]), spec.bugs.label)
        except GitHubError as e:
            log.info("could not label %s yet: %s", url, e)
    finally:
        store.close()
    return str(wo["id"])


def issue_body(url: str, repo: str | None = None) -> str:
    """The issue's text"""
    url = checked_issue_url(url, repo)
    try:
        payload = json.loads(_run(["issue", "view", url, "--json", "body"], url=url))
    except json.JSONDecodeError:
        return ""
    return str(payload.get("body") or "") if isinstance(payload, dict) else ""


def issue_priority(issue: Issue) -> str:
    """The level the tracker itself is carrying, or `""` if nobody has rated it"""
    for level in PRIORITIES:
        if issue.has(priority_label(level)):
            return level
    return ""


def issue_ref_url(target: str, repo: str) -> str:
    """`#123`, `123` or a whole URL — one issue URL"""
    from .ops import issue_url_for

    ref = (target or "").strip()
    if ISSUE_URL_RE.match(ref):
        return ref
    number = ref.lstrip("#")
    if not number.isdigit() or int(number) <= 0:
        raise IssueLifecycleError(
            f"{target!r} is neither an issue URL nor an issue number",
            GitHubError.URL_REFUSED)
    return issue_url_for(repo, int(number))


def start_work(target: str, project: str | None = None, *,
               metadata: dict[str, Any] | None = None,
               budget_usd: float | None = None,
               origin: str = "jarvis") -> dict[str, Any]:
    """Open a work order on a tracker issue"""
    from .ops import OpsError, follow_up_repo, registered_project_paths, resolve_catalog

    catalog = resolve_catalog()
    if project:
        spec = next((p for p in getattr(catalog, "projects", [])
                     if p.name == project), None)
        if spec is None:
            raise OpsError(f"project {project!r} is not in the catalog")
        repo = follow_up_repo(spec.path, spec.bugs.repo)
        if not repo:
            raise OpsError(f"project {project!r} has no git `origin` and no `bugs.repo`, "
                           f"so it owns no tracker to start an issue from")
    else:
        repo = bug_repo()
        spec = tracker_project(catalog, repo)
        if spec is None:
            raise OpsError(f"no registered project has {repo} as its git `origin` — "
                           f"name the project with --project")
    if spec.name not in registered_project_paths():
        raise OpsError(f"project {spec.name!r} is in the catalog but not registered — "
                       f"run `jarvis start` first")

    url = issue_ref_url(target, repo)
    issue = view(url, repo)
    if issue.closed:
        raise OpsError(f"{url} is closed — reopen it before starting work on it")
    level = issue_priority(issue)
    wo_id = promote_confirmed(
        spec, {"issue_url": url, "title": issue.title, "priority": level,
               "body": issue_body(url, repo)},
        why=STARTED_WHY.format(priority=level or "unrated"), metadata=metadata,
        budget_usd=budget_usd, origin=origin)
    return {"wo_id": wo_id, "project": spec.name, "issue_url": url, "priority": level}
